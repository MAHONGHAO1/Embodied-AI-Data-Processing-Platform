"""Safe, resumable intake primitives for Batch-owned import sessions."""

from __future__ import annotations

import hashlib
import os
import re
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from uuid import UUID, uuid4

from filelock import FileLock
from sqlalchemy import and_, func, or_, select
from sqlalchemy.orm import Session, selectinload

from data.config import settings
from data.database import (
    ArtifactOperation,
    Batch,
    CollectionDevice,
    EpisodeArtifact,
    ImportAttempt,
    ImportCandidate,
    ImportSession,
    JobRun,
    PersonnelProfile,
    TaskSetSourceImport,
    WorkspacePersonnelProfile,
)
from data.integrations.qrdf.paths import validate_qrdf_external_episode_id
from data.realtime.outbox import enqueue_resource_event
from data.realtime.projections import import_session_snapshot, job_run_snapshot
from data.services.artifact_operations import (
    cleanup_artifact_operation,
    complete_artifact_operation,
    ensure_artifact_operation,
    materialize_artifact_operation,
)
from data.services.batch_paths import raw_import_original_prefix
from data.services.capture_provenance import is_unknown_collector_identifier
from data.services.collector_profiles import available_collector_profile, collector_profile_item
from data.services.import_sessions import (
    TERMINAL_IMPORT_STATUSES,
    import_session_can_cleanup_originals,
)
from data.services.job_runs import NonRetryableJobError
from data.services.oss_import_scope import (
    OssImportScopeError,
    oss_import_scopes_for_batch,
    require_oss_import_scope,
)
from data.services.storage_mode import allow_oss_import, uses_cloud_uri_authority
from data.utils.formatting import format_api_datetime
from data.utils.storage_paths import is_under_storage_root, storage_root_path

MAX_IMPORT_CHUNKS = 20_000
MAX_IMPORT_CHUNK_BYTES = 64 * 1024 * 1024
MAX_IMPORT_TOTAL_BYTES = settings.import_max_upload_bytes
UPLOAD_CAPABLE_IMPORT_TYPES = frozenset({"chunked_upload", "duance_episode"})
# Browser pagination is deliberately independent from the worker's bounded
# scan budget.  Do not reuse this value in source discovery.
MAX_IMPORT_CANDIDATE_PAGE_SIZE = 500
MAX_IMPORT_CANDIDATE_SOURCE_GROUP_FACETS = 100
MAX_IMPORT_CANDIDATE_QUERY_SELECTION = 500
SOURCE_EPISODE_CANDIDATE_TYPES = frozenset({"ego_episode_oss", "capture_episode_oss"})
IMPORT_SCAN_JOB_KIND = "import_scan"
IMPORT_SCAN_QUEUE = "ingest"
IMPORT_MATERIALIZE_JOB_KIND = "import_materialize"
_UNSET_ATTRIBUTION = object()
_SAFE_SOURCE_FAILURE_CODES = frozenset(
    {
        "source_validation_failed",
        "raw_copy_data_failed",
        "raw_copy_metadata_failed",
        "raw_copy_complete_failed",
        "raw_publish_failed",
        "quality_job_failed",
        "import_parse_failed",
    }
)
_SOURCE_GROUP_STATUSES = frozenset({"valid", "missing", "invalid", "legacy"})
_SOURCE_GROUP_KEY_RE = re.compile(r"^(?:sg1|legacy_sg1)_[0-9a-f]{64}$")
_COLLECTOR_HINT_STATUSES = frozenset({"valid", "missing", "invalid"})
_COLLECTOR_IDENTIFIER_RE = re.compile(r"^[1-9][0-9]*$")


class CandidateMaterializationError(ValueError):
    """A candidate was valid to select but could not become a raw artifact."""


class ImportScanCandidateLimitError(NonRetryableJobError):
    """A bounded source scan found more candidates than the deployment permits."""


def start_import_candidate_scan(
    db: Session,
    *,
    import_session_id: str,
    source_date_from: date | None = None,
    source_date_to_exclusive: date | None = None,
) -> tuple[JobRun, bool]:
    """Create one durable candidate scan without touching OSS in the API process.

    A successful scan is an immutable snapshot for an ImportSession. A failed
    terminal scan can be started again with a new attempt; concurrent clicks
    always return the same active JobRun.
    """
    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == import_session_id).with_for_update()
    )
    if import_session is None:
        raise ValueError("import session does not exist")
    if import_session.import_type not in {"oss_scan", "filesystem_scan"}:
        raise ValueError("import session does not support source scanning")
    if import_session.status not in {"init", "failed"}:
        raise ValueError("import session is not ready to scan")

    result = dict(import_session.result_json or {})
    if _safe_scan_job_id(result.get("parse_job_id")):
        raise ValueError("import session parse retry is required")
    existing_job_id = _safe_scan_job_id(result.get("scan_job_id"))
    existing = db.get(JobRun, existing_job_id) if existing_job_id else None
    if (
        existing is not None
        and existing.kind == IMPORT_SCAN_JOB_KIND
        and existing.resource_id == import_session.id
    ):
        if existing.status not in {"failed", "cancelled"}:
            return existing, False

    batch = db.get(Batch, import_session.batch_id)
    if batch is None:
        raise ValueError("import session batch is unavailable")
    if import_session.import_type == "oss_scan":
        _ensure_oss_scan_date_range(
            import_session,
            source_date_from=source_date_from,
            source_date_to_exclusive=source_date_to_exclusive,
        )
    previous_attempt = _safe_nonnegative_int(result.get("scan_attempt"))
    attempt = previous_attempt + 1
    job = JobRun(
        id=uuid4().hex,
        kind=IMPORT_SCAN_JOB_KIND,
        resource_type="import_session",
        resource_id=import_session.id,
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
        idempotency_key=f"import-scan:{import_session.id}:{attempt}",
        queue=IMPORT_SCAN_QUEUE,
        actor_id=import_session.owner_user_id,
        status="queued",
        phase="queued",
        detail_json={"import_session_id": import_session.id, "scan_attempt": attempt},
    )
    db.add(job)
    db.flush()
    result.update(
        {
            "scan_job_id": job.id,
            "scan_status": "queued",
            "scan_attempt": attempt,
        }
    )
    result.pop("scan_error_code", None)
    result.pop("scan_candidate_count", None)
    import_session.result_json = result
    db.flush()
    return job, True


def safe_import_filename(file_name: str) -> str:
    """Accept one display filename, never a caller-controlled filesystem path."""
    if not isinstance(file_name, str) or not file_name or len(file_name) > 256:
        raise ValueError("invalid import file name")
    if (
        file_name != file_name.strip()
        or "\x00" in file_name
        or "/" in file_name
        or "\\" in file_name
    ):
        raise ValueError("invalid import file name")
    if file_name in {".", ".."} or Path(file_name).name != file_name:
        raise ValueError("invalid import file name")
    return file_name


def import_staging_dir(import_session_id: str) -> Path:
    """Return exactly one short-lived staging root below storage_root/imports."""
    session_id = _server_generated_session_id(import_session_id)
    root = storage_root_path()
    staging = (root / "imports" / session_id).resolve()
    if (
        not is_under_storage_root(staging, root=root)
        or staging.parent != (root / "imports").resolve()
    ):
        raise ValueError("import staging path is outside storage root")
    return staging


def resolve_import_original_key(
    *,
    workspace_id: int,
    task_set_id: int,
    batch_id: int,
    import_session_id: str,
    artifact_id: int,
    file_name: str,
) -> str:
    """Build the only raw object key permitted for an import original."""
    safe_name = safe_import_filename(file_name)
    prefix = raw_import_original_prefix(
        workspace_id=workspace_id,
        task_set_id=task_set_id,
        batch_id=batch_id,
        import_session_id=import_session_id,
        artifact_id=artifact_id,
    )
    return f"{prefix}/{safe_name}"


def start_import_upload(
    db: Session,
    *,
    import_session_id: str,
    file_name: str,
    total_chunks: int,
    file_size: int | None = None,
) -> dict[str, object]:
    """Create or resume one chunked-upload attempt without committing the caller's transaction."""
    return _start_resumable_upload(
        db,
        import_session_id=import_session_id,
        file_name=file_name,
        total_chunks=total_chunks,
        file_size=file_size,
    )


def direct_upload_capabilities() -> dict[str, object]:
    """Return non-secret browser upload limits for the current deployment."""
    from data.infra import oss_client

    return {
        "enabled": bool(
            uses_cloud_uri_authority() and oss_client.browser_multipart_upload_enabled()
        ),
        "threshold_bytes": settings.import_api_upload_max_bytes,
        "part_bytes": settings.import_direct_upload_part_bytes,
        "max_parts": 10_000,
        "concurrency": settings.import_direct_upload_concurrency,
    }


def start_direct_multipart_upload(
    db: Session,
    *,
    import_session_id: str,
    file_name: str,
    file_size: int,
) -> dict[str, object]:
    """Persist and initialize one exact OSS multipart upload for a large browser file."""
    from data.infra import oss_client

    safe_name = safe_import_filename(file_name)
    if isinstance(file_size, bool) or not isinstance(file_size, int):
        raise ValueError("import file size is invalid")
    if not settings.import_api_upload_max_bytes < file_size <= MAX_IMPORT_TOTAL_BYTES:
        raise ValueError("direct upload requires a file above the API upload limit")
    if not uses_cloud_uri_authority() or not oss_client.browser_multipart_upload_enabled():
        raise ValueError("browser direct upload is unavailable")
    part_size = settings.import_direct_upload_part_bytes
    total_parts = (file_size + part_size - 1) // part_size
    if not 1 <= total_parts <= 10_000:
        raise ValueError("direct upload exceeds the multipart part limit")

    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == import_session_id).with_for_update()
    )
    if import_session is None:
        raise ValueError("import session does not exist")
    if import_session.import_type not in UPLOAD_CAPABLE_IMPORT_TYPES:
        raise ValueError("import session does not accept browser upload")
    if import_session.status in TERMINAL_IMPORT_STATUSES:
        raise ValueError("import session is terminal")
    batch = db.get(Batch, import_session.batch_id)
    if batch is None:
        raise ValueError("import session batch is unavailable")
    active_attempt = db.scalar(
        select(ImportAttempt)
        .where(
            ImportAttempt.import_session_id == import_session.id, ImportAttempt.status == "active"
        )
        .order_by(ImportAttempt.created_at.desc(), ImportAttempt.id.desc())
        .with_for_update()
    )
    if active_attempt is not None:
        declaration = dict(active_attempt.result_json or {})
        if (
            declaration.get("upload_mode") != "oss_multipart"
            or declaration.get("file_name") != safe_name
            or declaration.get("file_size") != file_size
            or declaration.get("part_size") != part_size
            or declaration.get("total_parts") != total_parts
        ):
            raise ValueError("active import attempt has a different file declaration")
        artifact = db.get(EpisodeArtifact, declaration.get("artifact_id"))
        if artifact is None or artifact.import_session_id != import_session.id:
            raise ValueError("import artifact is unavailable")
    else:
        artifact = _create_import_artifact(
            db,
            import_session=import_session,
            batch=batch,
            file_name=safe_name,
        )
        object_key = _object_key_from_artifact(artifact)
        declaration = {
            "upload_mode": "oss_multipart",
            "artifact_id": artifact.id,
            "file_name": safe_name,
            "file_size": file_size,
            "part_size": part_size,
            "total_parts": total_parts,
            "bucket": oss_client.bucket_name("raw"),
            "object_key": object_key,
            "upload_id": "",
            "expires_at": format_api_datetime(
                datetime.utcnow() + timedelta(hours=settings.import_direct_upload_expire_hours)
            ),
        }
        active_attempt = ImportAttempt(
            import_session_id=import_session.id,
            attempt_token=uuid4().hex,
            status="active",
            result_json=declaration,
        )
        db.add(active_attempt)
        import_session.status = "uploading"
        import_session.original_name = safe_name
        db.flush()
        # The durable intent must exist before OSS creates external state.
        db.commit()

    declaration = dict(active_attempt.result_json or {})
    uploaded_parts: list[object] = []
    if declaration.get("upload_id"):
        uploaded_parts = _validated_direct_provider_parts(
            declaration,
            oss_client.list_browser_multipart_parts(
                str(declaration["bucket"]),
                str(declaration["object_key"]),
                str(declaration["upload_id"]),
            ),
            require_complete=False,
        )
    else:
        upload_id = oss_client.init_browser_multipart_upload(
            str(declaration["bucket"]), str(declaration["object_key"])
        )
        active_attempt = db.scalar(
            select(ImportAttempt).where(ImportAttempt.id == active_attempt.id).with_for_update()
        )
        if active_attempt is None or active_attempt.status != "active":
            oss_client.abort_browser_multipart_upload(
                str(declaration["bucket"]), str(declaration["object_key"]), upload_id
            )
            raise ValueError("active import attempt is unavailable")
        current = dict(active_attempt.result_json or {})
        if current.get("upload_id"):
            oss_client.abort_browser_multipart_upload(
                str(declaration["bucket"]), str(declaration["object_key"]), upload_id
            )
        else:
            current["upload_id"] = upload_id
            active_attempt.result_json = current
            declaration = current
            db.flush()
            db.commit()
    return _direct_attempt_view(import_session, active_attempt, uploaded_parts=uploaded_parts)


def sign_direct_multipart_part(
    db: Session,
    *,
    import_session_id: str,
    part_number: int,
) -> dict[str, object]:
    """Issue one short-lived UploadPart URL from server-owned state only."""
    from data.infra import oss_client

    import_session, attempt, artifact, declaration = _direct_upload_attempt(
        db, import_session_id=import_session_id
    )
    _require_direct_upload_not_expired(declaration)
    expected_size = _direct_part_size(declaration, part_number)
    _validate_direct_artifact_target(artifact, declaration)
    url, ttl, headers = oss_client.sign_browser_upload_part(
        str(declaration["bucket"]),
        str(declaration["object_key"]),
        str(declaration["upload_id"]),
        part_number,
    )
    return {
        "import_session_id": import_session.id,
        "attempt_id": attempt.id,
        "part_number": part_number,
        "content_length": expected_size,
        "headers": headers,
        "method": "PUT",
        "url": url,
        "expires_in": ttl,
    }


def complete_direct_multipart_upload(
    db: Session,
    *,
    import_session_id: str,
    parts: list[dict[str, object]],
) -> dict[str, object]:
    """Verify, complete and reconcile one browser multipart upload."""
    from data.infra import oss_client

    import_session, attempt, artifact, declaration = _direct_upload_attempt(
        db,
        import_session_id=import_session_id,
        allow_completed=True,
    )
    client_parts = _normalized_client_part_manifest(parts, declaration=declaration)
    if import_session.status == "uploaded" and attempt.status == "completed":
        parse_job_id = dict(import_session.result_json or {}).get("parse_job_id")
        if not isinstance(parse_job_id, str) or not parse_job_id:
            raise ValueError("import parse job is unavailable")
        return {
            "import_session_id": import_session.id,
            "status": "uploaded",
            "artifact_id": artifact.id,
            "parse_job_id": parse_job_id,
        }
    _require_direct_upload_not_expired(declaration)
    _validate_direct_artifact_target(artifact, declaration)
    bucket = str(declaration["bucket"])
    object_key = str(declaration["object_key"])
    upload_id = str(declaration["upload_id"])
    info = oss_client.object_info(bucket, object_key)
    stored_parts = (
        _normalized_client_part_manifest(
            list(declaration.get("completion_parts") or []), declaration=declaration
        )
        if declaration.get("completion_parts")
        else []
    )
    if info is None:
        provider_parts = _validated_direct_provider_parts(
            declaration,
            oss_client.list_browser_multipart_parts(bucket, object_key, upload_id),
            require_complete=True,
        )
        provider_manifest = [
            {"part_number": part.number, "etag": part.etag} for part in provider_parts
        ]
        if client_parts != provider_manifest:
            raise ValueError("multipart part manifest does not match the provider")
        declaration["completion_parts"] = provider_manifest
        declaration["completion_state"] = "completing"
        attempt.result_json = declaration
        db.flush()
        # Persist the verified provider manifest before CompleteMultipartUpload.
        db.commit()
        try:
            oss_client.complete_browser_multipart_upload(
                bucket, object_key, upload_id, provider_parts
            )
        except Exception:
            # CompleteMultipartUpload is ambiguous when its response is lost.
            # Accept only an object that the HEAD checks below can reconcile;
            # otherwise preserve the failure so the same durable intent retries.
            info = oss_client.object_info(bucket, object_key)
            if info is None:
                raise
        else:
            info = oss_client.object_info(bucket, object_key)
    elif not stored_parts or client_parts != stored_parts:
        raise ValueError("multipart part manifest does not match the persisted completion intent")
    if info is None or int(info.size) != int(declaration["file_size"]):
        raise ValueError("completed multipart object size does not match its declaration")
    crc64 = str(info.crc64 or "").strip()
    if not crc64.isdigit() or len(crc64) > 32:
        raise ValueError("completed multipart object is missing CRC64 integrity")

    # Reacquire rows after the pre-provider commit, then publish the exact
    # provider identity into the durable artifact ledger.
    import_session, attempt, artifact, declaration = _direct_upload_attempt(
        db,
        import_session_id=import_session_id,
        allow_completed=True,
    )
    if import_session.status == "uploaded" and attempt.status == "completed":
        parse_job_id = dict(import_session.result_json or {}).get("parse_job_id")
        if not isinstance(parse_job_id, str) or not parse_job_id:
            raise ValueError("import parse job is unavailable")
        return {
            "import_session_id": import_session.id,
            "status": "uploaded",
            "artifact_id": artifact.id,
            "parse_job_id": parse_job_id,
        }
    identity = _fingerprint("oss-multipart-v1", bucket, object_key, str(info.size), crc64)
    operation = ensure_artifact_operation(
        db,
        artifact=artifact,
        job=None,
        operation_kind="import_original_publish",
        checksum_sha256=identity,
        size_bytes=info.size,
        manifest_json={
            "kind": "file",
            "entries": [{"path": str(declaration["file_name"]), "size": info.size}],
            "keys": [],
        },
    )
    complete_artifact_operation(db, operation_id=operation.id)
    artifact.metadata_json = {
        **dict(artifact.metadata_json or {}),
        "integrity": {"algorithm": "crc64ecma", "value": crc64},
        "provider_etag": str(info.etag),
        "upload_mode": "oss_multipart",
    }
    declaration["completion_state"] = "completed"
    declaration["crc64"] = crc64
    result = _complete_materialized_import(
        db,
        import_session=import_session,
        attempt=attempt,
        artifact=artifact,
        checksum_sha256=identity,
        size_bytes=info.size,
        declaration=declaration,
    )
    return result


def expire_direct_multipart_upload(
    db: Session,
    *,
    import_session_id: str,
    now: datetime | None = None,
) -> bool:
    """Abort one expired, incomplete direct upload without deleting raw data."""
    from data.infra import oss_client

    import_session, attempt, artifact, declaration = _direct_upload_attempt(
        db, import_session_id=import_session_id
    )
    current = now or datetime.utcnow()
    if _direct_upload_expiry(declaration) > current:
        return False
    _validate_direct_artifact_target(artifact, declaration)
    bucket = str(declaration["bucket"])
    object_key = str(declaration["object_key"])
    if oss_client.object_info(bucket, object_key) is not None:
        return False
    oss_client.abort_browser_multipart_upload(bucket, object_key, str(declaration["upload_id"]))
    attempt.status = "cancelled"
    import_session.status = "cancelled"
    import_session.result_json = {"cancelled": True, "error_code": "direct_upload_expired"}
    db.flush()
    return True


def _start_resumable_upload(
    db: Session,
    *,
    import_session_id: str,
    file_name: str,
    total_chunks: int,
    file_size: int | None = None,
) -> dict[str, object]:
    safe_name = safe_import_filename(file_name)
    if (
        isinstance(total_chunks, bool)
        or not isinstance(total_chunks, int)
        or not 1 <= total_chunks <= MAX_IMPORT_CHUNKS
    ):
        raise ValueError("invalid import chunk count")
    if file_size is not None and (
        isinstance(file_size, bool)
        or not isinstance(file_size, int)
        or not 1 <= file_size <= settings.import_api_upload_max_bytes
    ):
        if isinstance(file_size, int) and file_size <= MAX_IMPORT_TOTAL_BYTES:
            raise ValueError("direct upload is required above the API upload limit")
        raise ValueError("import exceeds the configured upload limit")

    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == import_session_id).with_for_update()
    )
    if import_session is None:
        raise ValueError("import session does not exist")
    if import_session.import_type not in UPLOAD_CAPABLE_IMPORT_TYPES:
        raise ValueError("import session does not accept chunked upload")
    if import_session.status in TERMINAL_IMPORT_STATUSES:
        raise ValueError("import session is terminal")
    batch = db.get(Batch, import_session.batch_id)
    if batch is None:
        raise ValueError("import session batch is unavailable")

    active_attempt = db.scalar(
        select(ImportAttempt)
        .where(
            ImportAttempt.import_session_id == import_session.id, ImportAttempt.status == "active"
        )
        .order_by(ImportAttempt.created_at.desc(), ImportAttempt.id.desc())
    )
    if active_attempt is not None:
        declaration = dict(active_attempt.result_json or {})
        if (
            declaration.get("file_name") != safe_name
            or declaration.get("total_chunks") != total_chunks
        ):
            raise ValueError("active import attempt has a different file declaration")
        declared_size = declaration.get("file_size")
        if file_size is not None and declared_size not in {None, file_size}:
            raise ValueError("active import attempt has a different file declaration")
        if file_size is not None and declared_size is None:
            if int(declaration.get("uploaded_bytes") or 0) > file_size:
                raise ValueError("active import attempt exceeds the declared file size")
            declaration["file_size"] = file_size
            active_attempt.result_json = declaration
            db.flush()
        return _attempt_view(import_session, active_attempt)

    placeholder_uri = f"nas://pending-import/{import_session.id}/{uuid4().hex}"
    artifact = EpisodeArtifact(
        import_session_id=import_session.id,
        artifact_type="import_original",
        storage_role="raw",
        storage_uri=placeholder_uri,
        retention_policy="temporary",
        retention_until=import_session.retention_until,
    )
    db.add(artifact)
    db.flush()
    object_key = resolve_import_original_key(
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
        batch_id=batch.id,
        import_session_id=import_session.id,
        artifact_id=artifact.id,
        file_name=safe_name,
    )
    artifact.storage_uri = import_original_storage_uri(object_key)
    declaration = {
        "upload_mode": "api_chunked",
        "artifact_id": artifact.id,
        "file_name": safe_name,
        "total_chunks": total_chunks,
        "file_size": file_size,
        "uploaded_chunks": [],
        "uploaded_bytes": 0,
        "chunk_sizes": {},
    }
    attempt = ImportAttempt(
        import_session_id=import_session.id,
        attempt_token=uuid4().hex,
        status="active",
        result_json=declaration,
    )
    db.add(attempt)
    import_session.status = "uploading"
    import_session.original_name = safe_name
    db.flush()
    return _attempt_view(import_session, attempt)


def write_import_chunk(
    db: Session,
    *,
    import_session_id: str,
    chunk_index: int,
    content: bytes,
) -> dict[str, object]:
    """Persist one bounded chunk below the session-private staging root."""
    if isinstance(chunk_index, bool) or not isinstance(chunk_index, int) or chunk_index < 0:
        raise ValueError("invalid import chunk index")
    if not isinstance(content, bytes) or not content:
        raise ValueError("invalid import chunk content")
    if len(content) > MAX_IMPORT_CHUNK_BYTES:
        raise ValueError("import chunk exceeds the chunk size limit")

    import_session, attempt = _active_upload_attempt(db, import_session_id)
    declaration = dict(attempt.result_json or {})
    total_chunks = int(declaration["total_chunks"])
    if chunk_index >= total_chunks:
        raise ValueError("import chunk index is outside declared range")

    staging = import_staging_dir(import_session.id)
    chunks_dir = _safe_child(staging, "chunks")
    chunks_dir.mkdir(parents=True, exist_ok=True)
    chunk_path = _safe_child(chunks_dir, f"{chunk_index:08d}.part")
    lock = FileLock(str(_safe_child(staging, ".upload.lock")))
    with lock:
        uploaded = sorted({int(index) for index in declaration.get("uploaded_chunks") or []})
        if chunk_index in uploaded and chunk_path.is_file() and not chunk_path.is_symlink():
            return _attempt_view(import_session, attempt) | {"skipped": True}
        if chunk_index in uploaded:
            uploaded.remove(chunk_index)
            previous_size = int(dict(declaration.get("chunk_sizes") or {}).get(str(chunk_index), 0))
        else:
            previous_size = 0
        total_bytes = int(declaration.get("uploaded_bytes") or 0) - previous_size + len(content)
        if total_bytes > MAX_IMPORT_TOTAL_BYTES:
            raise ValueError("import exceeds the total size limit")
        declared_size = declaration.get("file_size")
        if declared_size is not None and total_bytes > int(declared_size):
            raise ValueError("import upload exceeds its declared file size")
        temp_path = _safe_child(chunks_dir, f".{chunk_index:08d}.{uuid4().hex}.tmp")
        try:
            with temp_path.open("xb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, chunk_path)
        finally:
            temp_path.unlink(missing_ok=True)

        uploaded.append(chunk_index)
        sizes = {
            str(key): int(value)
            for key, value in dict(declaration.get("chunk_sizes") or {}).items()
        }
        sizes[str(chunk_index)] = len(content)
        declaration["uploaded_chunks"] = sorted(uploaded)
        declaration["uploaded_bytes"] = total_bytes
        declaration["chunk_sizes"] = sizes
        attempt.result_json = declaration
        db.flush()
    return _attempt_view(import_session, attempt) | {"skipped": False}


def complete_import_upload(
    db: Session,
    *,
    import_session_id: str,
) -> dict[str, object]:
    """Validate the upload manifest and queue server-side materialization once."""
    import_session, attempt = _active_upload_attempt(db, import_session_id)
    declaration = dict(attempt.result_json or {})
    total_chunks = int(declaration["total_chunks"])
    uploaded = sorted({int(index) for index in declaration.get("uploaded_chunks") or []})
    expected = list(range(total_chunks))
    if uploaded != expected:
        raise ValueError("import upload is incomplete")
    declared_size = declaration.get("file_size")
    if declared_size is not None and int(declaration.get("uploaded_bytes") or 0) != int(
        declared_size
    ):
        raise ValueError("import upload size does not match its declaration")

    artifact_id = int(declaration["artifact_id"])
    artifact = db.get(EpisodeArtifact, artifact_id)
    if artifact is None or artifact.import_session_id != import_session.id:
        raise ValueError("import artifact is unavailable")
    batch = db.get(Batch, import_session.batch_id)
    if batch is None:
        raise ValueError("import session batch is unavailable")
    job = _get_or_create_materialize_job(
        db,
        import_session=import_session,
        batch=batch,
        attempt=attempt,
        artifact=artifact,
    )
    session_result = dict(import_session.result_json or {})
    session_result.update(
        {
            "artifact_id": artifact.id,
            "materialize_job_id": job.id,
            "materialize_status": "queued",
        }
    )
    import_session.result_json = session_result
    db.flush()
    return {
        "import_session_id": import_session.id,
        "status": "materialize_queued",
        "artifact_id": artifact.id,
        "materialize_job_id": job.id,
    }


def materialize_import_upload(db: Session, job: JobRun) -> dict[str, object]:
    """Assemble, hash and publish one completed chunk upload in worker-ingest."""
    if job.kind != IMPORT_MATERIALIZE_JOB_KIND or job.resource_type != "import_session":
        raise ValueError("import materialize job resource is invalid")
    detail = dict(job.detail_json or {})
    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == job.resource_id).with_for_update()
    )
    if import_session is None or detail.get("import_session_id") != import_session.id:
        raise ValueError("import session does not exist")
    attempt = db.get(ImportAttempt, _positive_int(detail.get("attempt_id"), "import attempt"))
    artifact = db.get(EpisodeArtifact, _positive_int(detail.get("artifact_id"), "import artifact"))
    if attempt is None or attempt.import_session_id != import_session.id:
        raise ValueError("active import attempt is unavailable")
    if artifact is None or artifact.import_session_id != import_session.id:
        raise ValueError("import artifact is unavailable")

    session_result = dict(import_session.result_json or {})
    existing_parse_job_id = session_result.get("parse_job_id")
    if import_session.status == "uploaded" and isinstance(existing_parse_job_id, str):
        return {
            "import_session_id": import_session.id,
            "status": "uploaded",
            "artifact_id": artifact.id,
            "parse_job_id": existing_parse_job_id,
            "_follow_up_job_ids": [existing_parse_job_id],
        }
    if import_session.status != "uploading" or attempt.status != "active":
        raise ValueError("import session is not ready for materialization")

    declaration = dict(attempt.result_json or {})
    total_chunks = _positive_int(declaration.get("total_chunks"), "import chunk count")
    expected = list(range(total_chunks))
    uploaded = sorted({int(index) for index in declaration.get("uploaded_chunks") or []})
    if uploaded != expected:
        raise ValueError("import upload is incomplete")
    staging = import_staging_dir(import_session.id)
    chunks_dir = _safe_child(staging, "chunks")
    assembled_path = _safe_child(staging, f".assembled-{attempt.attempt_token}.tmp")
    assembled_path.unlink(missing_ok=True)
    checksum, size_bytes = _assemble_chunks(
        chunks_dir=chunks_dir,
        chunk_indexes=expected,
        destination=assembled_path,
    )
    operation = ensure_artifact_operation(
        db,
        artifact=artifact,
        job=job,
        operation_kind="import_original_publish",
        source_path=assembled_path,
    )
    db.commit()
    _publish_assembled_import(
        assembled_path,
        object_key=_object_key_from_artifact(artifact),
        artifact=artifact,
        db=db,
        operation=operation,
    )
    result = _complete_materialized_import(
        db,
        import_session=import_session,
        attempt=attempt,
        artifact=artifact,
        checksum_sha256=checksum,
        size_bytes=size_bytes,
        declaration=declaration,
    )
    parse_job = db.get(JobRun, result["parse_job_id"])
    if parse_job is None:
        raise ValueError("import parse job is unavailable")
    enqueue_resource_event(
        db,
        resource=import_session,
        resource_type="import_session",
        event_name="import_session.updated",
        resource_snapshot=import_session_snapshot(import_session),
    )
    enqueue_resource_event(
        db,
        resource=parse_job,
        resource_type="job_run",
        event_name="job_run.updated",
        resource_snapshot=job_run_snapshot(parse_job),
    )
    db.commit()
    result["_follow_up_job_ids"] = [parse_job.id]
    return result


def scan_import_candidates(db: Session, *, import_session_id: str) -> list[dict[str, object]]:
    """Discover immutable, server-owned candidates without returning locators."""
    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == import_session_id).with_for_update()
    )
    if import_session is None:
        raise ValueError("import session does not exist")
    if import_session.import_type not in {"oss_scan", "filesystem_scan"}:
        raise ValueError("import session does not support source scanning")
    batch = db.get(Batch, import_session.batch_id)
    if batch is None:
        raise ValueError("import session batch is unavailable")
    existing = db.scalars(
        select(ImportCandidate)
        .where(ImportCandidate.import_session_id == import_session.id)
        .options(selectinload(ImportCandidate.task_set_source_import))
        .order_by(ImportCandidate.original_name, ImportCandidate.id)
    ).all()
    if existing:
        return _candidate_views(db, import_session=import_session, batch=batch, candidates=existing)
    if import_session.status not in {"init", "failed"}:
        raise ValueError("import session is not ready to scan")

    if import_session.import_type == "oss_scan":
        candidates = _scan_oss_candidates(db, import_session=import_session, batch=batch)
    else:
        candidates = _scan_filesystem_candidates(import_session=import_session, batch=batch)
    db.add_all(candidates)
    db.flush()
    return _candidate_views(db, import_session=import_session, batch=batch, candidates=candidates)


def run_import_candidate_scan(db: Session, job: JobRun) -> dict[str, object]:
    """Execute one durable source scan and publish only its safe completion state."""
    if job.kind != IMPORT_SCAN_JOB_KIND or job.resource_type != "import_session":
        raise ValueError("import scan job resource is invalid")
    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == job.resource_id).with_for_update()
    )
    if import_session is None:
        raise ValueError("import session does not exist")
    if not _set_import_scan_state(db, import_session=import_session, job=job, status="running"):
        return {"candidate_count": 0}
    db.commit()

    try:
        batch = db.get(Batch, import_session.batch_id)
        if batch is None:
            raise ValueError("import session batch is unavailable")
        partitioned_roots = _partitioned_capture_scan_roots(batch=batch)
        if import_session.import_type == "oss_scan" and partitioned_roots:
            candidate_count = _run_partitioned_capture_scan(
                db,
                import_session_id=import_session.id,
                job=job,
            )
            # A Batch may authorize the new V2 tree alongside historical EGO
            # or generic prefixes.  V2 uses its resumable date cursor; the
            # remaining scopes retain the compatibility scanner.  Do not let
            # the presence of one V2 root silently hide the other sources.
            legacy_candidates = _scan_oss_candidates(
                db,
                import_session=import_session,
                batch=batch,
                scopes=_legacy_oss_scan_scopes(batch=batch),
                excluded_capture_roots=set(partitioned_roots),
            )
            candidate_count = _persist_scanned_candidates(
                db,
                import_session_id=import_session.id,
                candidates=legacy_candidates,
                candidate_count=candidate_count,
            )
            db.commit()
        else:
            # Filesystem, legacy EGO, and flat source prefixes retain their
            # all-or-nothing compatibility scanner.  The V2 date layout uses
            # the resumable path above and never falls through to a full list.
            with db.begin_nested():
                candidates = scan_import_candidates(db, import_session_id=import_session.id)
            candidate_count = len(candidates)
    except ImportScanCandidateLimitError:
        db.rollback()
        current = db.scalar(
            select(ImportSession).where(ImportSession.id == job.resource_id).with_for_update()
        )
        if current is not None:
            _set_import_scan_state(
                db,
                import_session=current,
                job=job,
                status="failed",
                candidate_count=_import_candidate_count(db, import_session_id=current.id),
                error_code="import_scan_candidate_limit",
            )
            db.commit()
        raise
    except Exception:
        db.rollback()
        current = db.scalar(
            select(ImportSession).where(ImportSession.id == job.resource_id).with_for_update()
        )
        if current is not None:
            retry_pending = int(job.retry_count or 0) < 1
            _set_import_scan_state(
                db,
                import_session=current,
                job=job,
                status="retry_pending" if retry_pending else "failed",
                error_code=None if retry_pending else "import_scan_failed",
            )
            db.commit()
        raise

    current = db.scalar(
        select(ImportSession).where(ImportSession.id == job.resource_id).with_for_update()
    )
    if current is None:
        raise ValueError("import session does not exist")
    if _set_import_scan_state(
        db,
        import_session=current,
        job=job,
        status="succeeded",
        candidate_count=candidate_count,
    ):
        db.commit()
    return {"candidate_count": candidate_count}


def _ensure_oss_scan_date_range(
    import_session: ImportSession,
    *,
    source_date_from: date | None,
    source_date_to_exclusive: date | None,
) -> None:
    """Fix one bounded UTC date range for every attempt of an OSS scan."""
    existing_from = import_session.source_date_from
    existing_to = import_session.source_date_to_exclusive
    if (existing_from is None) != (existing_to is None):
        raise ValueError("import scan date range is invalid")
    if existing_from is not None and existing_to is not None:
        if source_date_from is not None and source_date_from != existing_from:
            raise ValueError("import scan date range is already fixed")
        if source_date_to_exclusive is not None and source_date_to_exclusive != existing_to:
            raise ValueError("import scan date range is already fixed")
        _validate_scan_date_range(existing_from, existing_to)
        if not import_session.scan_cursor_json:
            import_session.scan_cursor_json = _initial_scan_cursor(existing_from)
        return

    utc_today = datetime.now(timezone.utc).date()
    default_days = settings.import_scan_default_days
    if source_date_from is None and source_date_to_exclusive is None:
        normalized_to = utc_today + timedelta(days=1)
        normalized_from = normalized_to - timedelta(days=default_days)
    elif source_date_from is None:
        assert source_date_to_exclusive is not None
        normalized_to = source_date_to_exclusive
        normalized_from = normalized_to - timedelta(days=default_days)
    elif source_date_to_exclusive is None:
        normalized_from = source_date_from
        normalized_to = normalized_from + timedelta(days=default_days)
    else:
        normalized_from = source_date_from
        normalized_to = source_date_to_exclusive
    _validate_scan_date_range(normalized_from, normalized_to)
    import_session.source_date_from = normalized_from
    import_session.source_date_to_exclusive = normalized_to
    import_session.scan_cursor_json = _initial_scan_cursor(normalized_from)


def _validate_scan_date_range(source_date_from: date, source_date_to_exclusive: date) -> None:
    if isinstance(source_date_from, datetime) or isinstance(source_date_to_exclusive, datetime):
        raise ValueError("import scan dates must be calendar dates")
    if not isinstance(source_date_from, date) or not isinstance(source_date_to_exclusive, date):
        raise ValueError("import scan date range is invalid")
    span_days = (source_date_to_exclusive - source_date_from).days
    if not 1 <= span_days <= settings.import_scan_max_days:
        raise ValueError("import scan date range exceeds the configured limit")


def _initial_scan_cursor(source_date_from: date) -> dict[str, object]:
    return {
        "version": 1,
        # Existing V2 sources may still use the original flat directory
        # layout. Scan direct children once before the bounded date/hour tree.
        "phase": "capture_v2_flat",
        "scope_index": 0,
        "continuation_token": None,
    }


def _partitioned_capture_scan_roots(*, batch: Batch) -> list[tuple[str, str]]:
    """Return explicitly authorized V2 roots, sorted for a stable cursor.

    A broad prefix such as ``incoming`` remains on the legacy scanner.  Only
    an administrator-approved prefix ending in ``raw/v2/sources`` opts a
    session into date-partitioned scanning, so old trees cannot silently
    change behavior or consume the new cursor budget.
    """
    roots: set[tuple[str, str]] = set()
    for scope in oss_import_scopes_for_batch(batch=batch):
        bucket = scope.get("bucket")
        prefixes = scope.get("prefixes")
        if not isinstance(bucket, str) or not bucket or not isinstance(prefixes, list):
            continue
        for raw_prefix in prefixes:
            if not isinstance(raw_prefix, str):
                continue
            prefix = raw_prefix.strip().strip("/")
            if _is_partitioned_capture_root(prefix):
                roots.add((bucket, prefix))
    return sorted(roots)


def _is_partitioned_capture_root(prefix: str) -> bool:
    return prefix.split("/")[-3:] == ["raw", "v2", "sources"]


def _legacy_oss_scan_scopes(*, batch: Batch) -> list[dict[str, object]]:
    """Return non-V2 scope prefixes for the compatibility scanner.

    Prefixes are retained per bucket so one scope row can safely authorize a
    V2 source root and a historical EGO root at the same time.
    """
    scopes: list[dict[str, object]] = []
    for scope in oss_import_scopes_for_batch(batch=batch):
        bucket = scope.get("bucket")
        prefixes = scope.get("prefixes")
        if not isinstance(bucket, str) or not bucket or not isinstance(prefixes, list):
            continue
        legacy_prefixes = [
            prefix
            for prefix in prefixes
            if isinstance(prefix, str)
            and not _is_partitioned_capture_root(prefix.strip().strip("/"))
        ]
        if legacy_prefixes:
            scopes.append({"bucket": bucket, "prefixes": legacy_prefixes})
    return scopes


def _run_partitioned_capture_scan(
    db: Session,
    *,
    import_session_id: str,
    job: JobRun,
) -> int:
    """Advance a V2 date/hour/continuation cursor with one commit per page."""
    if not allow_oss_import():
        raise ValueError("external OSS import is not available in local storage mode")
    from data.infra import oss_client
    from data.services.capture_batch_import import scan_capture_episode_candidate

    while True:
        import_session = db.scalar(
            select(ImportSession).where(ImportSession.id == import_session_id).with_for_update()
        )
        if import_session is None:
            raise ValueError("import session does not exist")
        batch = db.get(Batch, import_session.batch_id)
        if batch is None:
            raise ValueError("import session batch is unavailable")
        if (
            import_session.source_date_from is None
            or import_session.source_date_to_exclusive is None
        ):
            raise ValueError("import scan date range is unavailable")
        _validate_scan_date_range(
            import_session.source_date_from, import_session.source_date_to_exclusive
        )
        roots = _partitioned_capture_scan_roots(batch=batch)
        if not roots:
            raise ValueError("import scan no longer has an authorized V2 source root")
        cursor = _validated_partitioned_scan_cursor(
            import_session.scan_cursor_json,
            source_date_from=import_session.source_date_from,
            source_date_to_exclusive=import_session.source_date_to_exclusive,
            scope_count=len(roots),
        )
        candidate_count = _import_candidate_count(db, import_session_id=import_session.id)
        if cursor["phase"] == "complete":
            return candidate_count

        scope_index = int(cursor["scope_index"])
        bucket, root = roots[scope_index]
        if cursor["phase"] == "capture_v2_flat":
            page = oss_client.list_prefix_directory_page(
                bucket,
                root,
                continuation_token=cursor["continuation_token"],
                max_keys=settings.import_scan_page_size,
            )
            object_keys = (
                complete_key
                for item in page.prefixes
                if (complete_key := _flat_capture_complete_key(root, item)) is not None
            )
        else:
            capture_day = date.fromisoformat(str(cursor["date"]))
            hour = int(cursor["hour"])
            page_prefix = f"{root}/date={capture_day.isoformat()}/hour={hour:02d}"
            # V2 packages are direct children of each UTC hour.  Listing
            # directories avoids reading every data/metadata/marker object
            # merely to find the one completion marker we will revalidate.
            page = oss_client.list_prefix_directory_page(
                bucket,
                page_prefix,
                continuation_token=cursor["continuation_token"],
                max_keys=settings.import_scan_page_size,
            )
            object_keys = (
                complete_key
                for item in page.prefixes
                if (complete_key := _flat_capture_complete_key(page_prefix, item)) is not None
            )

        added = 0
        for object_key in object_keys:
            added += int(
                _add_partitioned_capture_candidate(
                    db,
                    batch=batch,
                    import_session=import_session,
                    bucket=bucket,
                    object_key=object_key,
                    candidate_count=candidate_count + added,
                    scanner=scan_capture_episode_candidate,
                )
            )
        db.flush()

        next_cursor = _advance_partitioned_scan_cursor(
            cursor,
            source_date_from=import_session.source_date_from,
            source_date_to_exclusive=import_session.source_date_to_exclusive,
            scope_count=len(roots),
            continuation_token=page.next_token,
        )
        import_session.scan_cursor_json = next_cursor
        _set_import_scan_state(
            db,
            import_session=import_session,
            job=job,
            status="running",
            candidate_count=candidate_count + added,
            emit_realtime=added > 0,
        )
        # Candidate, source ledger, count, and cursor form one durable page.
        # A worker loss therefore resumes from the last committed page only.
        db.commit()


def _validated_partitioned_scan_cursor(
    value: object,
    *,
    source_date_from: date,
    source_date_to_exclusive: date,
    scope_count: int,
) -> dict[str, object]:
    if not isinstance(value, dict) or value.get("version") != 1:
        raise ValueError("import scan cursor is invalid")
    if value.get("phase") == "complete":
        return {"version": 1, "phase": "complete"}
    phase = value.get("phase")
    if phase not in {"capture_v2_flat", "capture_v2"}:
        raise ValueError("import scan cursor is invalid")
    scope_index = value.get("scope_index")
    token = value.get("continuation_token")
    if (
        isinstance(scope_index, bool)
        or not isinstance(scope_index, int)
        or not 0 <= scope_index < scope_count
    ):
        raise ValueError("import scan cursor is invalid")
    if token is not None and (
        not isinstance(token, str)
        or not 1 <= len(token) <= 2_048
        or any(ord(character) < 32 or ord(character) == 127 for character in token)
    ):
        raise ValueError("import scan cursor is invalid")
    if phase == "capture_v2_flat":
        return {
            "version": 1,
            "phase": "capture_v2_flat",
            "scope_index": scope_index,
            "continuation_token": token,
        }

    capture_day = value.get("date")
    hour = value.get("hour")
    if not isinstance(capture_day, str):
        raise ValueError("import scan cursor is invalid")
    try:
        parsed_day = date.fromisoformat(capture_day)
    except ValueError as exc:
        raise ValueError("import scan cursor is invalid") from exc
    if not source_date_from <= parsed_day < source_date_to_exclusive:
        raise ValueError("import scan cursor is invalid")
    if isinstance(hour, bool) or not isinstance(hour, int) or not 0 <= hour <= 23:
        raise ValueError("import scan cursor is invalid")
    return {
        "version": 1,
        "phase": "capture_v2",
        "scope_index": scope_index,
        "date": parsed_day.isoformat(),
        "hour": hour,
        "continuation_token": token,
    }


def _advance_partitioned_scan_cursor(
    cursor: dict[str, object],
    *,
    source_date_from: date,
    source_date_to_exclusive: date,
    scope_count: int,
    continuation_token: str | None,
) -> dict[str, object]:
    if continuation_token is not None:
        next_cursor = dict(cursor)
        next_cursor["continuation_token"] = continuation_token
        return next_cursor

    scope_index = int(cursor["scope_index"])
    if cursor["phase"] == "capture_v2_flat":
        if scope_index + 1 < scope_count:
            return {
                "version": 1,
                "phase": "capture_v2_flat",
                "scope_index": scope_index + 1,
                "continuation_token": None,
            }
        return {
            "version": 1,
            "phase": "capture_v2",
            "scope_index": 0,
            "date": source_date_from.isoformat(),
            "hour": 0,
            "continuation_token": None,
        }

    capture_day = date.fromisoformat(str(cursor["date"]))
    hour = int(cursor["hour"])
    if hour < 23:
        return {
            "version": 1,
            "phase": "capture_v2",
            "scope_index": scope_index,
            "date": capture_day.isoformat(),
            "hour": hour + 1,
            "continuation_token": None,
        }
    next_day = capture_day + timedelta(days=1)
    if next_day < source_date_to_exclusive:
        return {
            "version": 1,
            "phase": "capture_v2",
            "scope_index": scope_index,
            "date": next_day.isoformat(),
            "hour": 0,
            "continuation_token": None,
        }
    if scope_index + 1 >= scope_count:
        return {"version": 1, "phase": "complete"}
    return {
        "version": 1,
        "phase": "capture_v2",
        "scope_index": scope_index + 1,
        "date": source_date_from.isoformat(),
        "hour": 0,
        "continuation_token": None,
    }


def _flat_capture_complete_key(root: str, item: object) -> str | None:
    """Return a flat V2 package marker key for one immediate child prefix."""
    if (
        not isinstance(item, str)
        or not root
        or not item.startswith(f"{root}/")
        or not item.endswith("/")
    ):
        return None
    episode_id = item[len(root) + 1 : -1]
    if not episode_id or "/" in episode_id:
        return None
    try:
        validate_qrdf_external_episode_id(episode_id)
    except ValueError:
        return None
    return f"{root}/{episode_id}/complete.json"


def _add_partitioned_capture_candidate(
    db: Session,
    *,
    batch: Batch,
    import_session: ImportSession,
    bucket: str,
    object_key: object,
    candidate_count: int,
    scanner,
) -> bool:
    candidate = scanner(
        db,
        batch=batch,
        import_session=import_session,
        bucket=bucket,
        object_key=object_key,
    )
    if candidate is None:
        return False
    exists = db.scalar(
        select(ImportCandidate.id).where(
            ImportCandidate.import_session_id == import_session.id,
            ImportCandidate.source_fingerprint == candidate.source_fingerprint,
        )
    )
    if exists is not None:
        return False
    if candidate_count >= settings.import_scan_candidate_limit:
        raise ImportScanCandidateLimitError("import scan exceeds the candidate limit")
    db.add(candidate)
    return True


def _persist_scanned_candidates(
    db: Session,
    *,
    import_session_id: str,
    candidates: list[ImportCandidate],
    candidate_count: int,
) -> int:
    """Append legacy candidates without duplicating a resumed V2 scan."""
    for candidate in candidates:
        if candidate.import_session_id != import_session_id:
            raise ValueError("import candidate belongs to another session")
        exists = db.scalar(
            select(ImportCandidate.id).where(
                ImportCandidate.import_session_id == import_session_id,
                ImportCandidate.source_fingerprint == candidate.source_fingerprint,
            )
        )
        if exists is not None:
            continue
        if candidate_count >= settings.import_scan_candidate_limit:
            raise ImportScanCandidateLimitError("import scan exceeds the candidate limit")
        db.add(candidate)
        candidate_count += 1
    db.flush()
    return candidate_count


def _import_candidate_count(db: Session, *, import_session_id: str) -> int:
    value = db.scalar(
        select(func.count(ImportCandidate.id)).where(
            ImportCandidate.import_session_id == import_session_id
        )
    )
    return int(value or 0)


def _set_import_scan_state(
    db: Session,
    *,
    import_session: ImportSession,
    job: JobRun,
    status: str,
    candidate_count: int | None = None,
    error_code: str | None = None,
    emit_realtime: bool = True,
) -> bool:
    result = dict(import_session.result_json or {})
    if result.get("scan_job_id") != job.id:
        return False
    result["scan_status"] = status
    if candidate_count is not None:
        result["scan_candidate_count"] = candidate_count
    else:
        result.pop("scan_candidate_count", None)
    if error_code:
        result["scan_error_code"] = error_code
    else:
        result.pop("scan_error_code", None)
    import_session.result_json = result
    db.flush()
    if emit_realtime:
        enqueue_resource_event(
            db,
            resource=import_session,
            resource_type="import_session",
            event_name="import_session.updated",
            resource_snapshot=import_session_snapshot(import_session),
        )
    return True


def list_import_candidates(
    db: Session,
    *,
    import_session_id: str,
    source_status: str | None,
    captured_ended_after: datetime | None,
    captured_ended_before: datetime | None,
    limit: int,
    offset: int,
    source_group_key: str | None = None,
    source_group_status: str | None = None,
) -> tuple[list[dict[str, object]], int]:
    """Return one session's safe candidates with project-ledger status.

    The ledger owns EGO source state and its capture timestamp.  The browser
    only receives the display-safe projection, never the source locator.
    """
    if not 1 <= limit <= MAX_IMPORT_CANDIDATE_PAGE_SIZE or offset < 0:
        raise ValueError("import candidate pagination is invalid")
    if source_status is not None and source_status not in {
        "discovered",
        "importing",
        "imported",
        "failed",
    }:
        raise ValueError("import candidate source status is invalid")
    _validate_source_group_key(source_group_key)
    _validate_source_group_status(source_group_status)

    query = _import_candidate_query(
        db,
        import_session_id=import_session_id,
        source_status=source_status,
        captured_ended_after=captured_ended_after,
        captured_ended_before=captured_ended_before,
        source_group_key=source_group_key,
        source_group_status=source_group_status,
    )

    total = query.count()
    candidates = (
        query.options(selectinload(ImportCandidate.task_set_source_import))
        .order_by(
            TaskSetSourceImport.captured_ended_at.desc().nullslast(),
            ImportCandidate.original_name.asc(),
            ImportCandidate.id.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    import_session = db.get(ImportSession, import_session_id)
    if import_session is None:
        raise ValueError("import session does not exist")
    batch = db.get(Batch, import_session.batch_id)
    if batch is None:
        raise ValueError("import session batch is unavailable")
    return _candidate_views(
        db, import_session=import_session, batch=batch, candidates=candidates
    ), total


def list_import_candidate_source_group_facets(
    db: Session,
    *,
    import_session_id: str,
    source_status: str | None,
    captured_ended_after: datetime | None,
    captured_ended_before: datetime | None,
    source_group_status: str | None,
) -> dict[str, object]:
    """Return a bounded safe facet projection for one persisted snapshot.

    Facets deliberately do not apply an active ``source_group_key`` filter:
    users need to see adjacent groups in order to make a cross-page selection.
    The key, display name and status all come from explicit projection columns,
    never the candidate's private source locator.
    """
    _validate_source_group_status(source_group_status)
    query = _import_candidate_query(
        db,
        import_session_id=import_session_id,
        source_status=source_status,
        captured_ended_after=captured_ended_after,
        captured_ended_before=captured_ended_before,
        source_group_key=None,
        source_group_status=source_group_status,
    )
    grouped = query.with_entities(
        ImportCandidate.source_group_key,
        ImportCandidate.source_group_name,
        ImportCandidate.source_group_status,
        func.count(ImportCandidate.id),
    ).group_by(
        ImportCandidate.source_group_key,
        ImportCandidate.source_group_name,
        ImportCandidate.source_group_status,
    )
    total = grouped.order_by(None).count()
    rows = (
        grouped.order_by(
            ImportCandidate.source_group_status.asc(),
            ImportCandidate.source_group_name.asc().nullsfirst(),
            ImportCandidate.source_group_key.asc().nullsfirst(),
        )
        .limit(MAX_IMPORT_CANDIDATE_SOURCE_GROUP_FACETS + 1)
        .all()
    )
    truncated = len(rows) > MAX_IMPORT_CANDIDATE_SOURCE_GROUP_FACETS
    return {
        "items": [
            {
                "key": key,
                "name": name,
                "status": status,
                "count": int(count),
            }
            for key, name, status, count in rows[:MAX_IMPORT_CANDIDATE_SOURCE_GROUP_FACETS]
        ],
        "total": total,
        "truncated": truncated,
    }


def import_scanned_candidates_by_source_group_query(
    db: Session,
    *,
    import_session_id: str,
    source_group_keys: list[str],
    candidate_ids: list[str],
    source_group_statuses: list[str],
    exclude_candidate_ids: list[str],
    collector_profile_id: int | None | object = _UNSET_ATTRIBUTION,
    collection_device_id: int | None | object = _UNSET_ATTRIBUTION,
) -> list[dict[str, object]]:
    """Queue a bounded, server-recomputed selection of source-package rows.

    A browser never supplies an object locator or a page-local list of every
    selected id.  The server reuses the session-bound candidate snapshot and
    checks availability immediately before each transition, so a stale browser
    page cannot import an object outside its authorized ImportSession.
    """
    normalized_keys = _normalize_source_group_keys(source_group_keys)
    normalized_candidate_ids = _normalize_candidate_ids(candidate_ids)
    normalized_statuses = _normalize_source_group_statuses(source_group_statuses)
    excluded_ids = _normalize_candidate_ids(exclude_candidate_ids)
    if not normalized_keys and not normalized_candidate_ids:
        raise ValueError("import candidate selection is empty")
    query = _import_candidate_query(
        db,
        import_session_id=import_session_id,
        source_status=None,
        captured_ended_after=None,
        captured_ended_before=None,
        source_group_key=None,
        source_group_status=None,
    ).filter(
        ImportCandidate.candidate_type.in_(SOURCE_EPISODE_CANDIDATE_TYPES),
        ImportCandidate.status.in_(("discovered", "consumed")),
        TaskSetSourceImport.status.in_(("discovered", "failed")),
    )
    selection_filters = []
    if normalized_keys:
        group_filter = ImportCandidate.source_group_key.in_(normalized_keys)
        if normalized_statuses:
            group_filter = and_(
                group_filter,
                ImportCandidate.source_group_status.in_(normalized_statuses),
            )
        selection_filters.append(group_filter)
    if normalized_candidate_ids:
        selection_filters.append(ImportCandidate.id.in_(normalized_candidate_ids))
    query = query.filter(or_(*selection_filters))
    if excluded_ids:
        query = query.filter(~ImportCandidate.id.in_(excluded_ids))

    total = query.count()
    if total <= 0:
        raise ValueError("import candidate selection is empty")
    if total > MAX_IMPORT_CANDIDATE_QUERY_SELECTION:
        raise ValueError(
            f"import candidate selection exceeds the limit of {MAX_IMPORT_CANDIDATE_QUERY_SELECTION}"
        )
    candidates = (
        query.options(selectinload(ImportCandidate.task_set_source_import))
        .order_by(
            TaskSetSourceImport.captured_ended_at.desc().nullslast(),
            ImportCandidate.original_name.asc(),
            ImportCandidate.id.asc(),
        )
        .all()
    )
    results: list[dict[str, object]] = []
    for candidate in candidates:
        results.append(
            import_scanned_candidate(
                db,
                import_session_id=import_session_id,
                candidate_id=candidate.id,
                collector_profile_id=collector_profile_id,
                collection_device_id=collection_device_id,
            )
        )
    return results


def _import_candidate_query(
    db: Session,
    *,
    import_session_id: str,
    source_status: str | None,
    captured_ended_after: datetime | None,
    captured_ended_before: datetime | None,
    source_group_key: str | None,
    source_group_status: str | None,
):
    query = (
        db.query(ImportCandidate)
        .outerjoin(
            TaskSetSourceImport, ImportCandidate.task_set_source_import_id == TaskSetSourceImport.id
        )
        .filter(ImportCandidate.import_session_id == import_session_id)
    )
    if source_status is not None:
        query = query.filter(TaskSetSourceImport.status == source_status)
    normalized_after = _normalize_candidate_timestamp(captured_ended_after)
    normalized_before = _normalize_candidate_timestamp(captured_ended_before)
    if normalized_after is not None:
        query = query.filter(TaskSetSourceImport.captured_ended_at >= normalized_after)
    if normalized_before is not None:
        query = query.filter(TaskSetSourceImport.captured_ended_at < normalized_before)
    if source_group_key is not None:
        query = query.filter(ImportCandidate.source_group_key == source_group_key)
    if source_group_status is not None:
        query = query.filter(ImportCandidate.source_group_status == source_group_status)
    return query


def _validate_source_group_key(value: str | None) -> None:
    if value is not None and not _SOURCE_GROUP_KEY_RE.fullmatch(value):
        raise ValueError("import candidate source group key is invalid")


def _validate_source_group_status(value: str | None) -> None:
    if value is not None and value not in _SOURCE_GROUP_STATUSES:
        raise ValueError("import candidate source group status is invalid")


def _normalize_source_group_keys(values: list[str]) -> tuple[str, ...]:
    # A query may select only explicit candidate ids. The caller verifies that
    # at least one group or candidate is present after both inputs are parsed.
    if len(values) > 50:
        raise ValueError("import candidate source group selection is invalid")
    normalized = tuple(dict.fromkeys(str(value or "") for value in values))
    if len(normalized) != len(values):
        raise ValueError("import candidate source group selection is invalid")
    for value in normalized:
        _validate_source_group_key(value)
    return normalized


def _normalize_source_group_statuses(values: list[str]) -> tuple[str, ...]:
    if len(values) > len(_SOURCE_GROUP_STATUSES):
        raise ValueError("import candidate source group status selection is invalid")
    normalized = tuple(dict.fromkeys(str(value or "") for value in values))
    if len(normalized) != len(values):
        raise ValueError("import candidate source group status selection is invalid")
    for value in normalized:
        _validate_source_group_status(value)
    return normalized


def _normalize_candidate_ids(values: list[str]) -> tuple[str, ...]:
    if len(values) > MAX_IMPORT_CANDIDATE_QUERY_SELECTION:
        raise ValueError("import candidate exclusion is too large")
    normalized = tuple(dict.fromkeys(str(value or "") for value in values))
    if len(normalized) != len(values) or any(
        not _is_server_generated_candidate_id(value) for value in normalized
    ):
        raise ValueError("import candidate exclusion is invalid")
    return normalized


def _normalize_candidate_timestamp(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _safe_scan_job_id(value: object) -> str | None:
    if not isinstance(value, str) or len(value) != 32:
        return None
    if not all(character in "0123456789abcdef" for character in value):
        return None
    return value


def _safe_nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return 0
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return 0
    return parsed if 0 <= parsed <= 1_000_000 else 0


def import_scanned_candidate(
    db: Session,
    *,
    import_session_id: str,
    candidate_id: str,
    collector_profile_id: int | None | object = _UNSET_ATTRIBUTION,
    collection_device_id: int | None | object = _UNSET_ATTRIBUTION,
) -> dict[str, object]:
    """Promote one scanned source to the canonical Batch raw namespace."""
    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == import_session_id).with_for_update()
    )
    if import_session is None:
        raise ValueError("import session does not exist")
    if import_session.import_type not in {"oss_scan", "filesystem_scan"}:
        raise ValueError("import session does not support scanned candidates")
    if import_session.status not in {"init", "uploading", "uploaded", "parsing", "failed"}:
        raise ValueError("import session is not ready to import a candidate")
    candidate = db.scalar(
        select(ImportCandidate)
        .where(
            ImportCandidate.id == candidate_id,
            ImportCandidate.import_session_id == import_session.id,
        )
        .with_for_update()
    )
    if candidate is None:
        raise ValueError("import candidate does not exist")
    # One Batch can have multiple ImportSessions. Lock its provenance row before
    # binding a grouped legacy task so concurrent sessions cannot mix sources.
    batch = db.scalar(select(Batch).where(Batch.id == import_session.batch_id).with_for_update())
    if batch is None:
        raise ValueError("import session batch is unavailable")
    reported_collector = _require_importable_reported_collector(
        db,
        batch=batch,
        candidate=candidate,
    )
    attribution_override = _validated_candidate_attribution_override(
        db,
        batch=batch,
        collector_profile_id=_UNSET_ATTRIBUTION
        if reported_collector is not None
        else collector_profile_id,
        collection_device_id=collection_device_id,
    )
    if reported_collector is not None:
        # A valid device-reported ID is an attribution fact. The form value is
        # only a default for packages that did not report an operator at all.
        attribution_override["collector_profile_id"] = None

    if candidate.candidate_type in SOURCE_EPISODE_CANDIDATE_TYPES:
        return _queue_source_episode_candidate_import(
            db,
            import_session=import_session,
            batch=batch,
            candidate=candidate,
            attribution_override=attribution_override,
        )

    if import_session.status not in {"init", "uploading", "failed"}:
        raise ValueError("import session is not ready to import a candidate")

    artifact, attempt, declaration = _start_or_resume_scanned_candidate_attempt(
        db,
        import_session=import_session,
        batch=batch,
        candidate=candidate,
        attribution_override=attribution_override,
    )

    try:
        if candidate.candidate_type == "oss_object":
            checksum_sha256, size_bytes, source_fingerprint = _copy_oss_candidate(
                db,
                batch=batch,
                candidate=candidate,
                artifact=artifact,
            )
        elif candidate.candidate_type == "filesystem_file":
            assembled_path, checksum_sha256, size_bytes = _copy_filesystem_candidate_to_staging(
                batch=batch,
                import_session=import_session,
                candidate=candidate,
            )
            operation = ensure_artifact_operation(
                db,
                artifact=artifact,
                job=None,
                operation_kind="import_original_publish",
                source_path=assembled_path,
            )
            # The source snapshot, artifact and exact target are durable before
            # a local promotion or OSS write can leave the request process.
            db.commit()
            _publish_assembled_import(
                assembled_path,
                object_key=_object_key_from_artifact(artifact),
                artifact=artifact,
                db=db,
                operation=operation,
            )
            source_fingerprint = checksum_sha256
        else:
            raise ValueError("import candidate has an unsupported type")
    except (OssImportScopeError, OSError, ValueError) as exc:
        attempt.status = "failed"
        attempt.error_code = "candidate_import_failed"
        attempt.result_json = declaration
        import_session.status = "failed"
        import_session.result_json = {"error_code": "candidate_import_failed"}
        db.flush()
        raise CandidateMaterializationError("import candidate could not be materialized") from exc

    import_session.original_name = candidate.original_name
    import_session.source_fingerprint = source_fingerprint
    return _complete_materialized_import(
        db,
        import_session=import_session,
        attempt=attempt,
        artifact=artifact,
        checksum_sha256=checksum_sha256,
        size_bytes=size_bytes,
        declaration=declaration,
    )


def _start_or_resume_scanned_candidate_attempt(
    db: Session,
    *,
    import_session: ImportSession,
    batch: Batch,
    candidate: ImportCandidate,
    attribution_override: dict[str, int | None],
) -> tuple[EpisodeArtifact, ImportAttempt, dict[str, object]]:
    """Fence generic candidate materialization around one durable artifact."""
    attempts = list(
        db.scalars(
            select(ImportAttempt)
            .where(ImportAttempt.import_session_id == import_session.id)
            .order_by(ImportAttempt.created_at.desc(), ImportAttempt.id.desc())
            .with_for_update()
        )
    )
    for attempt in attempts:
        declaration = dict(attempt.result_json or {})
        if declaration.get("candidate_id") != candidate.id:
            continue
        artifact_id = declaration.get("artifact_id")
        artifact = db.get(EpisodeArtifact, artifact_id) if isinstance(artifact_id, int) else None
        if artifact is None or artifact.import_session_id != import_session.id:
            raise ValueError("candidate import artifact is unavailable")
        if candidate.status != "consumed" or attempt.status not in {"active", "failed"}:
            raise ValueError("import candidate is no longer available")
        if declaration.get("attribution_override", {}) != attribution_override:
            raise ValueError("candidate attribution override does not match the existing attempt")
        attempt.status = "active"
        attempt.error_code = ""
        import_session.status = "uploading"
        db.flush()
        return artifact, attempt, declaration

    unfinished = next(
        (
            attempt
            for attempt in attempts
            if attempt.status in {"active", "failed"}
            and isinstance(dict(attempt.result_json or {}).get("candidate_id"), str)
        ),
        None,
    )
    if unfinished is not None:
        raise ValueError("import session has an unfinished candidate materialization")
    if import_session.status not in {"init", "failed"} or candidate.status != "discovered":
        raise ValueError("import candidate is no longer available")

    artifact = _create_import_artifact(
        db,
        import_session=import_session,
        batch=batch,
        file_name=candidate.original_name,
    )
    declaration = {
        "artifact_id": artifact.id,
        "candidate_id": candidate.id,
        "file_name": candidate.original_name,
        "attribution_override": attribution_override,
    }
    attempt = ImportAttempt(
        import_session_id=import_session.id,
        attempt_token=uuid4().hex,
        status="active",
        result_json=declaration,
    )
    db.add(attempt)
    candidate.status = "consumed"
    import_session.status = "uploading"
    db.flush()
    return artifact, attempt, declaration


def _queue_source_episode_candidate_import(
    db: Session,
    *,
    import_session: ImportSession,
    batch: Batch,
    candidate: ImportCandidate,
    attribution_override: dict[str, int | None],
) -> dict[str, object]:
    """Reserve one source-package ledger row; the worker owns all OSS copying."""
    if candidate.status not in {"discovered", "consumed"}:
        raise ValueError("import candidate is no longer available")
    ledger_id = candidate.task_set_source_import_id
    if not isinstance(ledger_id, int) or ledger_id <= 0:
        raise ValueError("source package ledger is unavailable")
    ledger = db.scalar(
        select(TaskSetSourceImport)
        .where(
            TaskSetSourceImport.id == ledger_id,
            TaskSetSourceImport.task_set_id == batch.task_set_id,
        )
        .with_for_update()
    )
    if ledger is None:
        raise ValueError("source package ledger is unavailable")
    if ledger.status in {"importing", "imported"}:
        raise ValueError("source package has already been selected for import")
    if candidate.status == "consumed" and ledger.status != "failed":
        raise ValueError("import candidate is no longer available")

    attempt = ImportAttempt(
        import_session_id=import_session.id,
        attempt_token=uuid4().hex,
        status="completed",
        result_json={
            "candidate_id": candidate.id,
            "candidate_type": candidate.candidate_type,
            "attribution_override": attribution_override,
        },
    )
    db.add(attempt)
    db.flush()

    candidate.status = "consumed"
    ledger.status = "importing"
    ledger.batch_id = batch.id
    ledger.import_session_id = import_session.id
    ledger.error_code = ""
    ledger.error_message = ""
    import_session.original_name = candidate.original_name
    import_session.source_fingerprint = candidate.source_fingerprint
    parse_job = _get_or_create_parse_job(
        db,
        import_session=import_session,
        batch=batch,
        attempt=attempt,
    )
    import_session.status = "uploaded"
    import_session.result_json = {
        **dict(import_session.result_json or {}),
        "parse_job_id": parse_job.id,
    }
    db.flush()
    return {
        "import_session_id": import_session.id,
        "status": import_session.status,
        "artifact_id": None,
        "parse_job_id": parse_job.id,
    }


def cancel_import_upload(db: Session, *, import_session_id: str) -> dict[str, object]:
    """Cancel pre-raw intake and remove only its exact staging directory."""
    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == import_session_id).with_for_update()
    )
    if import_session is None:
        raise ValueError("import session does not exist")
    if import_session.status not in {"init", "uploading", "failed"}:
        raise ValueError("import session cannot be cancelled")
    active_attempt = db.scalar(
        select(ImportAttempt)
        .where(
            ImportAttempt.import_session_id == import_session.id, ImportAttempt.status == "active"
        )
        .with_for_update()
    )
    if active_attempt is not None:
        declaration = dict(active_attempt.result_json or {})
        artifact_id = declaration.get("artifact_id")
        artifact = db.get(EpisodeArtifact, artifact_id) if artifact_id else None
        if artifact is not None and artifact.import_session_id == import_session.id:
            if import_session.import_type in {"oss_scan", "filesystem_scan"}:
                raise ValueError("candidate materialization is in progress")
            materialize_pending = db.scalar(
                select(JobRun.id).where(
                    JobRun.kind == IMPORT_MATERIALIZE_JOB_KIND,
                    JobRun.resource_type == "import_session",
                    JobRun.resource_id == import_session.id,
                    JobRun.status.in_(("queued", "running", "retry_pending")),
                )
            )
            if materialize_pending is not None:
                raise ValueError("import materialization is in progress")
            operations = list(
                db.scalars(
                    select(ArtifactOperation)
                    .where(ArtifactOperation.artifact_id == artifact.id)
                    .with_for_update()
                )
            )
            if any(operation.status == "published" for operation in operations):
                raise ValueError("published raw imports cannot be cancelled")
            if declaration.get("upload_mode") == "oss_multipart":
                from data.infra import oss_client

                _validate_direct_artifact_target(artifact, declaration)
                bucket = str(declaration["bucket"])
                object_key = str(declaration["object_key"])
                if oss_client.object_info(bucket, object_key) is not None:
                    raise ValueError("published raw imports cannot be cancelled")
                oss_client.abort_browser_multipart_upload(
                    bucket,
                    object_key,
                    str(declaration["upload_id"]),
                )
            for operation in operations:
                if operation.status in {"pending", "cleanup_pending"}:
                    cleanup_artifact_operation(db, operation_id=operation.id)
                if operation.status == "cleaned":
                    db.delete(operation)
            db.flush()
            db.delete(artifact)
        active_attempt.status = "cancelled"
    import_session.status = "cancelled"
    import_session.result_json = {"cancelled": True}
    _remove_exact_import_staging(import_session.id)
    db.flush()
    return {"import_session_id": import_session.id, "status": import_session.status}


def mark_terminal_import_materialize_failure(db: Session, job: JobRun) -> ImportSession:
    """Expose a terminal materialization failure without deleting resumable bytes."""
    if job.kind != IMPORT_MATERIALIZE_JOB_KIND or job.resource_type != "import_session":
        raise ValueError("import materialize job resource is invalid")
    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == job.resource_id).with_for_update()
    )
    if import_session is None:
        raise ValueError("import session does not exist")
    if import_session.status == "uploading":
        result = dict(import_session.result_json or {})
        result.update(
            {
                "materialize_job_id": job.id,
                "materialize_status": "failed",
                "error_code": "import_materialize_failed",
            }
        )
        import_session.result_json = result
        import_session.status = "failed"
        enqueue_resource_event(
            db,
            resource=import_session,
            resource_type="import_session",
            event_name="import_session.updated",
            resource_snapshot=import_session_snapshot(import_session),
        )
        db.flush()
    return import_session


def cleanup_expired_import_staging(
    db: Session, *, import_session_id: str, now: datetime | None = None
) -> bool:
    """Delete one expired session's temporary staging data, never its raw artifact prefix."""
    import_session = db.get(ImportSession, import_session_id)
    if import_session is None:
        raise ValueError("import session does not exist")
    if not import_session_can_cleanup_originals(import_session, now=now or datetime.utcnow()):
        return False
    _remove_exact_import_staging(import_session.id)
    return True


def retry_import_parse(db: Session, *, import_session_id: str) -> dict[str, object]:
    """Queue a fenced parse retry for an already materialized raw original."""
    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == import_session_id).with_for_update()
    )
    if import_session is None:
        raise ValueError("import session does not exist")
    if import_session.status != "failed":
        raise ValueError("import session is not retryable")
    batch = db.get(Batch, import_session.batch_id)
    if batch is None:
        raise ValueError("import session batch is unavailable")
    attempt = db.scalar(
        select(ImportAttempt)
        .where(
            ImportAttempt.import_session_id == import_session.id,
            ImportAttempt.status == "completed",
        )
        .order_by(ImportAttempt.created_at.desc(), ImportAttempt.id.desc())
        .with_for_update()
    )
    if attempt is None:
        raise ValueError("import session has no completed raw attempt")
    parse_detail = _parse_job_detail(import_session=import_session, attempt=attempt)
    artifact_id = parse_detail.get("artifact_id")
    if isinstance(artifact_id, int):
        artifact = db.get(EpisodeArtifact, artifact_id)
        if artifact is None or artifact.import_session_id != import_session.id:
            raise ValueError("import artifact is unavailable")
    else:
        candidate_id = parse_detail.get("candidate_id")
        candidate = db.get(ImportCandidate, candidate_id) if isinstance(candidate_id, str) else None
        if (
            candidate is None
            or candidate.import_session_id != import_session.id
            or candidate.candidate_type not in SOURCE_EPISODE_CANDIDATE_TYPES
            or candidate.status != "consumed"
        ):
            raise ValueError("source package candidate is unavailable")
    base_key = f"import-parse:{import_session.id}:{attempt.attempt_token}"
    retry_count = (
        db.query(JobRun).filter(JobRun.idempotency_key.startswith(f"{base_key}:retry:")).count()
    )
    retry_job = JobRun(
        id=uuid4().hex,
        kind="import_parse",
        resource_type="import_session",
        resource_id=import_session.id,
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
        idempotency_key=f"{base_key}:retry:{retry_count + 1}",
        queue="ingest",
        actor_id=import_session.owner_user_id,
        status="queued",
        phase="queued",
        detail_json=parse_detail,
    )
    db.add(retry_job)
    import_session.status = "uploaded"
    retry_result: dict[str, object] = {"parse_job_id": retry_job.id}
    if isinstance(artifact_id, int):
        retry_result["artifact_id"] = artifact_id
    if import_session.import_type == "duance_episode":
        prior_result = (
            import_session.result_json if isinstance(import_session.result_json, dict) else {}
        )
        manifest = prior_result.get("duance_manifest")
        if isinstance(manifest, dict):
            retry_result["duance_manifest"] = manifest
    import_session.result_json = retry_result
    db.flush()
    return {
        "import_session_id": import_session.id,
        "status": import_session.status,
        "artifact_id": artifact_id if isinstance(artifact_id, int) else None,
        "parse_job_id": retry_job.id,
    }


def _candidate_views(
    db: Session,
    *,
    import_session: ImportSession,
    batch: Batch,
    candidates: list[ImportCandidate],
) -> list[dict[str, object]]:
    reported_keys = {
        str(candidate.reported_collector_identifier)
        for candidate in candidates
        if candidate.candidate_type in SOURCE_EPISODE_CANDIDATE_TYPES
        and str(candidate.collector_hint_status or "") == "valid"
        and isinstance(candidate.reported_collector_identifier, str)
        and _COLLECTOR_IDENTIFIER_RE.fullmatch(candidate.reported_collector_identifier)
    }
    profiles_by_key: dict[str, PersonnelProfile] = {}
    if reported_keys:
        profiles_by_key = {
            profile.profile_key: profile
            for profile in db.scalars(
                select(PersonnelProfile)
                .join(
                    WorkspacePersonnelProfile,
                    WorkspacePersonnelProfile.personnel_profile_id == PersonnelProfile.id,
                )
                .where(
                    WorkspacePersonnelProfile.workspace_id == batch.workspace_id,
                    PersonnelProfile.profile_key.in_(reported_keys),
                )
            )
        }
    default_collector = (
        available_collector_profile(
            db,
            workspace_id=batch.workspace_id,
            profile_id=import_session.default_collector_profile_id,
        )
        if import_session.default_collector_profile_id is not None
        else None
    )
    return [
        _candidate_view(
            candidate,
            default_collector=default_collector,
            profiles_by_key=profiles_by_key,
        )
        for candidate in candidates
    ]


def _candidate_view(
    candidate: ImportCandidate,
    *,
    default_collector: PersonnelProfile | None = None,
    profiles_by_key: dict[str, PersonnelProfile] | None = None,
) -> dict[str, object]:
    """The complete source locator is intentionally not part of this DTO."""
    view: dict[str, object] = {
        "id": candidate.id,
        "candidate_type": candidate.candidate_type,
        "status": candidate.status,
        "original_name": candidate.original_name,
        "size_bytes": candidate.size_bytes,
    }
    if candidate.candidate_type == "ego_episode_oss":
        legacy_source_group = _ego_episode_candidate_summary(candidate)
        ledger = candidate.task_set_source_import
        if ledger is None:
            raise ValueError("EGO source ledger is unavailable")
        view["legacy_source_group"] = legacy_source_group
        view["source_group"] = _candidate_source_group_view(candidate)
        view["source_status"] = ledger.status
        view["captured_ended_at"] = format_api_datetime(ledger.captured_ended_at)
        if ledger.status == "failed" and ledger.error_code in _SAFE_SOURCE_FAILURE_CODES:
            # Only expose a bounded code. Historical error_message values may
            # predate this redaction boundary and must remain server-side.
            view["failure_code"] = ledger.error_code
    elif candidate.candidate_type == "capture_episode_oss":
        ledger = candidate.task_set_source_import
        if ledger is None:
            raise ValueError("capture source ledger is unavailable")
        view["source_group"] = _candidate_source_group_view(candidate)
        view["collector_attribution"] = _candidate_collector_attribution_view(
            candidate,
            default_collector=default_collector,
            profiles_by_key=profiles_by_key or {},
        )
        view["source_status"] = ledger.status
        view["captured_ended_at"] = format_api_datetime(ledger.captured_ended_at)
        if ledger.status == "failed" and ledger.error_code in _SAFE_SOURCE_FAILURE_CODES:
            view["failure_code"] = ledger.error_code
    return view


def _candidate_collector_attribution_view(
    candidate: ImportCandidate,
    *,
    default_collector: PersonnelProfile | None,
    profiles_by_key: dict[str, PersonnelProfile],
) -> dict[str, object]:
    """Describe scan-time attribution without exposing its private locator."""
    hint_status = str(candidate.collector_hint_status or "missing")
    reported_identifier = (
        candidate.reported_collector_identifier
        if isinstance(candidate.reported_collector_identifier, str)
        else ""
    )
    # Older scan snapshots persisted EGO's ``unknown`` sentinel as an
    # invalid reported identifier before the producer contract was aligned.
    # Normalize those rows at the read boundary so they behave like new scans.
    if is_unknown_collector_identifier(reported_identifier):
        hint_status = "missing"
        reported_identifier = ""
    if hint_status == "invalid":
        return {
            "source": "machine_reported",
            "state": "format_error",
            "reported_identifier": reported_identifier,
            "profile": None,
            "importable": False,
            "error_code": "collector_id_invalid",
        }
    if hint_status == "valid" and _COLLECTOR_IDENTIFIER_RE.fullmatch(reported_identifier):
        profile = profiles_by_key.get(reported_identifier)
        if profile is not None:
            return {
                "source": "machine_reported",
                "state": "automatic",
                "reported_identifier": reported_identifier,
                "profile": collector_profile_item(profile),
                "importable": True,
                "error_code": "",
            }
        return {
            "source": "machine_reported",
            "state": "mapping_error",
            "reported_identifier": reported_identifier,
            "profile": None,
            "importable": False,
            "error_code": "collector_not_found",
        }
    if hint_status != "missing":
        return {
            "source": "machine_reported",
            "state": "format_error",
            "reported_identifier": reported_identifier,
            "profile": None,
            "importable": False,
            "error_code": "collector_id_invalid",
        }
    if default_collector is not None:
        return {
            "source": "default",
            "state": "default",
            "reported_identifier": "",
            "profile": collector_profile_item(default_collector),
            "importable": True,
            "error_code": "",
        }
    return {
        "source": "unknown",
        "state": "unreported",
        "reported_identifier": "",
        "profile": None,
        "importable": True,
        "error_code": "",
    }


def _candidate_source_group_view(candidate: ImportCandidate) -> dict[str, str | None]:
    """Project only persisted, reader-validated source group fields."""
    status = str(candidate.source_group_status or "missing")
    if status not in _SOURCE_GROUP_STATUSES:
        status = "invalid"
    key = candidate.source_group_key if isinstance(candidate.source_group_key, str) else None
    name = candidate.source_group_name if isinstance(candidate.source_group_name, str) else None
    if status in {"valid", "legacy"} and (not key or not name):
        status = "invalid"
        key = None
        name = None
    if status in {"missing", "invalid"}:
        key = None
        name = None
    return {"key": key, "name": name, "status": status}


def _ego_episode_candidate_summary(candidate: ImportCandidate) -> str:
    """Return only the display-safe legacy group, never an OSS locator."""
    locator = candidate.locator_json
    if not isinstance(locator, dict):
        raise ValueError("EGO source candidate locator is invalid")
    try:
        return safe_import_filename(locator.get("legacy_source_group"))
    except ValueError as exc:
        raise ValueError("EGO source candidate lineage is invalid") from exc


def _scan_oss_candidates(
    db: Session,
    *,
    import_session: ImportSession,
    batch: Batch,
    scopes: list[dict[str, object]] | None = None,
    excluded_capture_roots: set[tuple[str, str]] | None = None,
) -> list[ImportCandidate]:
    if not allow_oss_import():
        raise ValueError("external OSS import is not available in local storage mode")
    from data.infra import oss_client
    from data.services.capture_batch_import import (
        is_capture_source_object_key,
        scan_capture_episode_candidates,
    )
    from data.services.historical_ego_import import (
        is_legacy_ego_object_key,
        scan_ego_episode_candidates,
    )

    candidates: list[ImportCandidate] = []
    seen_fingerprints: set[str] = set()
    for scope in scopes if scopes is not None else oss_import_scopes_for_batch(batch=batch):
        bucket = str(scope["bucket"])
        prefixes = scope["prefixes"]
        if not isinstance(prefixes, list):
            continue
        for prefix in prefixes:
            object_limit = settings.import_scan_legacy_object_limit
            objects = oss_client.list_prefix(bucket, str(prefix), limit=object_limit)
            if len(objects) >= object_limit:
                raise ValueError("import scan exceeds the source object limit")
            if excluded_capture_roots:
                objects = [
                    item
                    for item in objects
                    if not _object_is_under_capture_root(
                        bucket=bucket,
                        key=item.get("key") if isinstance(item, dict) else None,
                        roots=excluded_capture_roots,
                    )
                ]
            legacy_candidates = scan_ego_episode_candidates(
                db,
                batch=batch,
                import_session=import_session,
                bucket=bucket,
                objects=objects,
            )
            for legacy_candidate in legacy_candidates:
                if legacy_candidate.source_fingerprint in seen_fingerprints:
                    continue
                if len(candidates) >= settings.import_scan_candidate_limit:
                    raise ImportScanCandidateLimitError("import scan exceeds the candidate limit")
                candidates.append(legacy_candidate)
                seen_fingerprints.add(legacy_candidate.source_fingerprint)
            capture_candidates = scan_capture_episode_candidates(
                db,
                batch=batch,
                import_session=import_session,
                bucket=bucket,
                objects=objects,
            )
            for capture_candidate in capture_candidates:
                if capture_candidate.source_fingerprint in seen_fingerprints:
                    continue
                if len(candidates) >= settings.import_scan_candidate_limit:
                    raise ImportScanCandidateLimitError("import scan exceeds the candidate limit")
                candidates.append(capture_candidate)
                seen_fingerprints.add(capture_candidate.source_fingerprint)
            for item in objects:
                if not isinstance(item, dict):
                    continue
                key = str(item.get("key") or "")
                try:
                    if is_legacy_ego_object_key(key) or is_capture_source_object_key(key):
                        continue
                    require_oss_import_scope(batch=batch, bucket=bucket, keys=[key])
                    original_name = safe_import_filename(key.rsplit("/", 1)[-1])
                    size_bytes = _bounded_candidate_size(item.get("size"))
                except (OssImportScopeError, ValueError):
                    continue
                fingerprint = _fingerprint("oss", bucket, key, str(size_bytes))
                if fingerprint in seen_fingerprints:
                    continue
                if len(candidates) >= settings.import_scan_candidate_limit:
                    raise ImportScanCandidateLimitError("import scan exceeds the candidate limit")
                candidates.append(
                    ImportCandidate(
                        id=str(uuid4()),
                        import_session_id=import_session.id,
                        candidate_type="oss_object",
                        original_name=original_name,
                        size_bytes=size_bytes,
                        source_fingerprint=fingerprint,
                        locator_json={"bucket": bucket, "key": key},
                    )
                )
                seen_fingerprints.add(fingerprint)
    return candidates


def _object_is_under_capture_root(
    *,
    bucket: str,
    key: object,
    roots: set[tuple[str, str]],
) -> bool:
    if not isinstance(key, str):
        return False
    return any(
        bucket == root_bucket and (key == root_prefix or key.startswith(f"{root_prefix}/"))
        for root_bucket, root_prefix in roots
    )


def filesystem_candidate_inbox(*, batch: Batch) -> Path:
    """Return the only server-managed, non-recursive filesystem inbox for a Batch."""
    root = storage_root_path()
    inbox = (
        root
        / "import-candidates"
        / "workspaces"
        / str(batch.workspace_id)
        / "task-sets"
        / str(batch.task_set_id)
    )
    current = root
    for part in inbox.relative_to(root).parts:
        current = current / part
        if current.exists() and current.is_symlink():
            raise ValueError("filesystem import inbox contains a symlink")
    resolved = inbox.resolve()
    if not is_under_storage_root(resolved, root=root):
        raise ValueError("filesystem import inbox is outside storage root")
    return inbox


def _scan_filesystem_candidates(
    *, import_session: ImportSession, batch: Batch
) -> list[ImportCandidate]:
    inbox = filesystem_candidate_inbox(batch=batch)
    if not inbox.exists():
        return []
    if not inbox.is_dir() or inbox.is_symlink():
        raise ValueError("filesystem import inbox is not a directory")

    candidates: list[ImportCandidate] = []
    with os.scandir(inbox) as entries:
        for entry in entries:
            if len(candidates) >= settings.import_scan_candidate_limit:
                raise ImportScanCandidateLimitError("import scan exceeds the candidate limit")
            if not entry.is_file(follow_symlinks=False):
                continue
            try:
                original_name = safe_import_filename(entry.name)
                stat = entry.stat(follow_symlinks=False)
                size_bytes = _bounded_candidate_size(stat.st_size)
            except (OSError, ValueError):
                continue
            fingerprint = _fingerprint(
                "filesystem",
                original_name,
                str(stat.st_dev),
                str(stat.st_ino),
                str(stat.st_mtime_ns),
                str(stat.st_size),
            )
            candidates.append(
                ImportCandidate(
                    id=str(uuid4()),
                    import_session_id=import_session.id,
                    candidate_type="filesystem_file",
                    original_name=original_name,
                    size_bytes=size_bytes,
                    source_fingerprint=fingerprint,
                    locator_json={
                        "file_name": original_name,
                        "st_dev": stat.st_dev,
                        "st_ino": stat.st_ino,
                        "st_mtime_ns": stat.st_mtime_ns,
                        "st_size": stat.st_size,
                    },
                )
            )
    return candidates


def _copy_oss_candidate(
    db: Session,
    *,
    batch: Batch,
    candidate: ImportCandidate,
    artifact: EpisodeArtifact,
) -> tuple[str, int, str]:
    if not allow_oss_import():
        raise ValueError("external OSS import is not available in local storage mode")
    locator = dict(candidate.locator_json or {})
    bucket = str(locator.get("bucket") or "")
    key = str(locator.get("key") or "")
    require_oss_import_scope(batch=batch, bucket=bucket, keys=[key])

    from data.infra import oss_client

    source = oss_client.object_info(bucket, key)
    if source is None or source.size != candidate.size_bytes or not source.etag:
        raise ValueError("scanned OSS source changed before import")
    object_key = _object_key_from_artifact(artifact)
    raw_bucket = oss_client.bucket_name("raw")
    source_fingerprint = _fingerprint("oss", bucket, key, source.etag, source.version_id or "")
    operation = ensure_artifact_operation(
        db,
        artifact=artifact,
        job=None,
        operation_kind="import_original_publish",
        checksum_sha256=source_fingerprint,
        size_bytes=source.size,
        manifest_json={
            "kind": "file",
            "entries": [{"path": candidate.original_name, "size": source.size}],
            "keys": [],
        },
    )
    # Preserve the exact source identity and target before CopyObject. A retry
    # can safely finish this operation after a process or database failure.
    db.commit()

    if operation.status != "published":
        target = (
            oss_client.object_info(raw_bucket, object_key)
            if oss_client.object_exists(raw_bucket, object_key)
            else None
        )
        if target is None:
            try:
                oss_client.copy_object(
                    bucket,
                    key,
                    raw_bucket,
                    object_key,
                    source_etag=source.etag,
                    source_version_id=source.version_id,
                    forbid_overwrite=True,
                )
            except FileExistsError:
                # A second recovery request may race after the first one wrote
                # the immutable key but before it marked the operation done.
                pass
            target = oss_client.object_info(raw_bucket, object_key)
        if target is None or not _oss_copy_target_matches_source(target, source):
            raise ValueError("canonical import object identity could not be verified")
        complete_artifact_operation(db, operation_id=operation.id)
        db.commit()
    return "", source.size, source_fingerprint


def _copy_filesystem_candidate_to_staging(
    *,
    batch: Batch,
    import_session: ImportSession,
    candidate: ImportCandidate,
) -> tuple[Path, str, int]:
    locator = dict(candidate.locator_json or {})
    file_name = safe_import_filename(str(locator.get("file_name") or ""))
    inbox = filesystem_candidate_inbox(batch=batch)
    source = inbox / file_name
    staging = import_staging_dir(import_session.id)
    staging.mkdir(parents=True, exist_ok=True)
    target = _safe_child(staging, f".candidate-{candidate.id}.tmp")
    lock = FileLock(str(_safe_child(staging, ".upload.lock")))
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)

    with lock:
        try:
            descriptor = os.open(source, flags)
        except OSError as exc:
            raise ValueError("filesystem candidate is unavailable") from exc
        try:
            stat = os.fstat(descriptor)
            expected = ("st_dev", "st_ino", "st_mtime_ns", "st_size")
            if any(int(locator.get(key, -1)) != int(getattr(stat, key)) for key in expected):
                raise ValueError("filesystem candidate changed before import")
            _bounded_candidate_size(stat.st_size)
            digest = hashlib.sha256()
            with (
                os.fdopen(descriptor, "rb", closefd=False) as source_file,
                target.open("xb") as target_file,
            ):
                while block := source_file.read(1024 * 1024):
                    target_file.write(block)
                    digest.update(block)
                target_file.flush()
                os.fsync(target_file.fileno())
        except Exception:
            target.unlink(missing_ok=True)
            raise
        finally:
            os.close(descriptor)
    return target, digest.hexdigest(), stat.st_size


def _create_import_artifact(
    db: Session,
    *,
    import_session: ImportSession,
    batch: Batch,
    file_name: str,
) -> EpisodeArtifact:
    safe_name = safe_import_filename(file_name)
    artifact = EpisodeArtifact(
        import_session_id=import_session.id,
        artifact_type="import_original",
        storage_role="raw",
        storage_uri=f"nas://pending-import/{import_session.id}/{uuid4().hex}",
        retention_policy="temporary",
        retention_until=import_session.retention_until,
    )
    db.add(artifact)
    db.flush()
    artifact.storage_uri = import_original_storage_uri(
        resolve_import_original_key(
            workspace_id=batch.workspace_id,
            task_set_id=batch.task_set_id,
            batch_id=batch.id,
            import_session_id=import_session.id,
            artifact_id=artifact.id,
            file_name=safe_name,
        )
    )
    return artifact


def _complete_materialized_import(
    db: Session,
    *,
    import_session: ImportSession,
    attempt: ImportAttempt,
    artifact: EpisodeArtifact,
    checksum_sha256: str,
    size_bytes: int,
    declaration: dict[str, object],
) -> dict[str, object]:
    batch = db.get(Batch, import_session.batch_id)
    if batch is None:
        raise ValueError("import session batch is unavailable")
    artifact.checksum_sha256 = checksum_sha256
    artifact.size_bytes = size_bytes
    artifact.retention_policy = "permanent"
    artifact.retention_until = None
    attempt.status = "completed"
    declaration["checksum_sha256"] = checksum_sha256
    declaration["size_bytes"] = size_bytes
    attempt.result_json = declaration
    import_session.status = "uploaded"
    session_result = dict(import_session.result_json or {})
    session_result.update(
        {
            "artifact_id": artifact.id,
            "materialize_status": "completed",
            "parse_job_id": _get_or_create_parse_job(
                db,
                import_session=import_session,
                batch=batch,
                attempt=attempt,
            ).id,
        }
    )
    import_session.result_json = session_result
    db.flush()
    return {
        "import_session_id": import_session.id,
        "status": import_session.status,
        "artifact_id": artifact.id,
        "parse_job_id": import_session.result_json["parse_job_id"],
    }


def _get_or_create_materialize_job(
    db: Session,
    *,
    import_session: ImportSession,
    batch: Batch,
    attempt: ImportAttempt,
    artifact: EpisodeArtifact,
) -> JobRun:
    idempotency_key = f"import-materialize:{import_session.id}:{attempt.attempt_token}"
    existing = db.scalar(select(JobRun).where(JobRun.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    job = JobRun(
        id=uuid4().hex,
        kind=IMPORT_MATERIALIZE_JOB_KIND,
        resource_type="import_session",
        resource_id=import_session.id,
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
        idempotency_key=idempotency_key,
        queue="ingest",
        actor_id=import_session.owner_user_id,
        status="queued",
        phase="queued",
        detail_json={
            "import_session_id": import_session.id,
            "attempt_id": attempt.id,
            "artifact_id": artifact.id,
        },
    )
    db.add(job)
    db.flush()
    return job


def _positive_int(value: object, label: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} is invalid")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed <= 0:
        raise ValueError(f"{label} is invalid")
    return parsed


def _fingerprint(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode("utf-8")).hexdigest()


def _oss_copy_target_matches_source(target, source) -> bool:
    """Require immutable object identity after a resumable server-side copy."""
    if int(target.size) != int(source.size):
        return False
    if _normalize_oss_identity(target.etag) != _normalize_oss_identity(source.etag):
        return False
    if source.crc64 and _normalize_oss_identity(target.crc64 or "") != _normalize_oss_identity(
        source.crc64
    ):
        return False
    return True


def _normalize_oss_identity(value: object) -> str:
    normalized = str(value or "").strip()
    if len(normalized) >= 2 and normalized.startswith('"') and normalized.endswith('"'):
        return normalized[1:-1]
    return normalized


def _bounded_candidate_size(value: object) -> int:
    if isinstance(value, bool):
        raise ValueError("import candidate size is invalid")
    try:
        size = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("import candidate size is invalid") from exc
    if not 0 < size <= MAX_IMPORT_TOTAL_BYTES:
        raise ValueError("import candidate size is invalid")
    return size


def _attempt_view(import_session: ImportSession, attempt: ImportAttempt) -> dict[str, object]:
    declaration = dict(attempt.result_json or {})
    return {
        "import_session_id": import_session.id,
        "attempt_id": attempt.id,
        "artifact_id": declaration["artifact_id"],
        "file_name": declaration["file_name"],
        "status": import_session.status,
        "total_chunks": declaration["total_chunks"],
        "uploaded_chunks": list(declaration.get("uploaded_chunks") or []),
    }


def _direct_attempt_view(
    import_session: ImportSession,
    attempt: ImportAttempt,
    *,
    uploaded_parts: list[object],
) -> dict[str, object]:
    declaration = dict(attempt.result_json or {})
    return {
        "import_session_id": import_session.id,
        "attempt_id": attempt.id,
        "artifact_id": declaration["artifact_id"],
        "file_name": declaration["file_name"],
        "status": import_session.status,
        "upload_mode": "oss_multipart",
        "part_size": declaration["part_size"],
        "total_parts": declaration["total_parts"],
        "uploaded_parts": [
            {
                "part_number": int(part.number),
                "etag": str(part.etag),
                "size": int(part.size),
            }
            for part in uploaded_parts
        ],
        "expires_at": declaration["expires_at"],
    }


def _direct_upload_attempt(
    db: Session,
    *,
    import_session_id: str,
    allow_completed: bool = False,
) -> tuple[ImportSession, ImportAttempt, EpisodeArtifact, dict[str, object]]:
    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == import_session_id).with_for_update()
    )
    if import_session is None:
        raise ValueError("import session does not exist")
    allowed_session_statuses = {"uploading", "uploaded"} if allow_completed else {"uploading"}
    if (
        import_session.import_type not in UPLOAD_CAPABLE_IMPORT_TYPES
        or import_session.status not in allowed_session_statuses
    ):
        raise ValueError("import session is not accepting a direct upload")
    statuses = ("active", "completed") if allow_completed else ("active",)
    attempt = db.scalar(
        select(ImportAttempt)
        .where(
            ImportAttempt.import_session_id == import_session.id,
            ImportAttempt.status.in_(statuses),
        )
        .order_by(ImportAttempt.created_at.desc(), ImportAttempt.id.desc())
        .with_for_update()
    )
    if attempt is None:
        raise ValueError("direct import attempt is unavailable")
    declaration = dict(attempt.result_json or {})
    if declaration.get("upload_mode") != "oss_multipart":
        raise ValueError("import attempt does not accept direct upload")
    artifact_id = declaration.get("artifact_id")
    artifact = db.get(EpisodeArtifact, artifact_id) if type(artifact_id) is int else None
    if artifact is None or artifact.import_session_id != import_session.id:
        raise ValueError("import artifact is unavailable")
    if not declaration.get("upload_id"):
        raise ValueError("direct multipart upload is not initialized")
    _validate_direct_artifact_target(artifact, declaration)
    return import_session, attempt, artifact, declaration


def _validate_direct_artifact_target(
    artifact: EpisodeArtifact,
    declaration: dict[str, object],
) -> None:
    from data.infra import oss_client

    expected_bucket = oss_client.bucket_name("raw")
    expected_key = _object_key_from_artifact(artifact)
    if (
        declaration.get("bucket") != expected_bucket
        or declaration.get("object_key") != expected_key
        or artifact.storage_uri != f"oss://{expected_bucket}/{expected_key}"
    ):
        raise ValueError("direct upload target does not match its import artifact")


def _direct_upload_expiry(declaration: dict[str, object]) -> datetime:
    raw = declaration.get("expires_at")
    if not isinstance(raw, str) or not raw:
        raise ValueError("direct upload expiry is invalid")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("direct upload expiry is invalid") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _require_direct_upload_not_expired(declaration: dict[str, object]) -> None:
    if _direct_upload_expiry(declaration) <= datetime.utcnow():
        raise ValueError("direct multipart upload has expired")


def _direct_part_size(declaration: dict[str, object], part_number: int) -> int:
    if isinstance(part_number, bool) or not isinstance(part_number, int):
        raise ValueError("multipart part number is invalid")
    total_parts = _positive_int(declaration.get("total_parts"), "multipart part count")
    if not 1 <= part_number <= total_parts:
        raise ValueError("multipart part number is outside the declared range")
    file_size = _positive_int(declaration.get("file_size"), "import file size")
    part_size = _positive_int(declaration.get("part_size"), "multipart part size")
    if part_number < total_parts:
        return part_size
    return file_size - part_size * (total_parts - 1)


def _normalize_direct_etag(value: object) -> str:
    etag = str(value or "").strip()
    if len(etag) >= 2 and etag.startswith('"') and etag.endswith('"'):
        etag = etag[1:-1]
    if (
        not 1 <= len(etag) <= 256
        or not etag.isascii()
        or any(not character.isprintable() for character in etag)
    ):
        raise ValueError("multipart part ETag is invalid")
    return etag


def _normalized_client_part_manifest(
    parts: list[dict[str, object]],
    *,
    declaration: dict[str, object],
) -> list[dict[str, object]]:
    if not isinstance(parts, list):
        raise ValueError("multipart part manifest is invalid")
    total_parts = _positive_int(declaration.get("total_parts"), "multipart part count")
    if len(parts) != total_parts:
        raise ValueError("multipart part manifest is incomplete")
    normalized: list[dict[str, object]] = []
    for expected_number, part in enumerate(parts, start=1):
        if not isinstance(part, dict) or set(part) != {"part_number", "etag"}:
            raise ValueError("multipart part manifest is invalid")
        if type(part.get("part_number")) is not int or part["part_number"] != expected_number:
            raise ValueError("multipart part manifest is invalid")
        normalized.append(
            {
                "part_number": expected_number,
                "etag": _normalize_direct_etag(part.get("etag")),
            }
        )
    return normalized


def _validated_direct_provider_parts(
    declaration: dict[str, object],
    parts: list[object],
    *,
    require_complete: bool,
) -> list[object]:
    total_parts = _positive_int(declaration.get("total_parts"), "multipart part count")
    if not isinstance(parts, list) or len(parts) > total_parts:
        raise ValueError("provider multipart part manifest is invalid")
    expected_numbers = list(range(1, total_parts + 1)) if require_complete else None
    numbers = [int(getattr(part, "number", 0)) for part in parts]
    if numbers != sorted(set(numbers)) or (
        expected_numbers is not None and numbers != expected_numbers
    ):
        raise ValueError("provider multipart part manifest is incomplete")
    for part in parts:
        number = int(getattr(part, "number", 0))
        if not 1 <= number <= total_parts:
            raise ValueError("provider multipart part manifest is invalid")
        if int(getattr(part, "size", 0)) != _direct_part_size(declaration, number):
            raise ValueError("provider multipart part size does not match its declaration")
        _normalize_direct_etag(getattr(part, "etag", None))
    return parts


def _active_upload_attempt(
    db: Session, import_session_id: str
) -> tuple[ImportSession, ImportAttempt]:
    import_session = db.scalar(
        select(ImportSession).where(ImportSession.id == import_session_id).with_for_update()
    )
    if import_session is None:
        raise ValueError("import session does not exist")
    if import_session.import_type not in UPLOAD_CAPABLE_IMPORT_TYPES:
        raise ValueError("import session does not accept chunked upload")
    if import_session.status != "uploading":
        raise ValueError("import session is not uploading")
    attempt = db.scalar(
        select(ImportAttempt)
        .where(
            ImportAttempt.import_session_id == import_session.id, ImportAttempt.status == "active"
        )
        .order_by(ImportAttempt.created_at.desc(), ImportAttempt.id.desc())
        .with_for_update()
    )
    if attempt is None:
        raise ValueError("active import attempt is unavailable")
    if dict(attempt.result_json or {}).get("upload_mode", "api_chunked") != "api_chunked":
        raise ValueError("import session requires direct multipart upload")
    return import_session, attempt


def _assemble_chunks(
    *, chunks_dir: Path, chunk_indexes: list[int], destination: Path
) -> tuple[str, int]:
    digest = hashlib.sha256()
    size_bytes = 0
    try:
        with destination.open("xb") as target:
            for index in chunk_indexes:
                chunk_path = _safe_child(chunks_dir, f"{index:08d}.part")
                if not chunk_path.is_file() or chunk_path.is_symlink():
                    raise ValueError("import upload contains an invalid chunk")
                with chunk_path.open("rb") as source:
                    while block := source.read(1024 * 1024):
                        target.write(block)
                        digest.update(block)
                        size_bytes += len(block)
            target.flush()
            os.fsync(target.fileno())
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return digest.hexdigest(), size_bytes


def _get_or_create_parse_job(
    db: Session,
    *,
    import_session: ImportSession,
    batch: Batch,
    attempt: ImportAttempt,
) -> JobRun:
    idempotency_key = f"import-parse:{import_session.id}:{attempt.attempt_token}"
    existing = db.scalar(select(JobRun).where(JobRun.idempotency_key == idempotency_key))
    if existing is not None:
        return existing
    job = JobRun(
        id=uuid4().hex,
        kind="import_parse",
        resource_type="import_session",
        resource_id=import_session.id,
        workspace_id=batch.workspace_id,
        task_set_id=batch.task_set_id,
        idempotency_key=idempotency_key,
        queue="ingest",
        actor_id=import_session.owner_user_id,
        status="queued",
        phase="queued",
        detail_json=_parse_job_detail(import_session=import_session, attempt=attempt),
    )
    db.add(job)
    db.flush()
    return job


def _parse_job_detail(
    *, import_session: ImportSession, attempt: ImportAttempt
) -> dict[str, object]:
    declaration = dict(attempt.result_json or {})
    attribution_override = _safe_attribution_override(declaration.get("attribution_override", {}))
    candidate_type = declaration.get("candidate_type")
    candidate_id = declaration.get("candidate_id")
    if candidate_type is not None:
        if (
            candidate_type not in SOURCE_EPISODE_CANDIDATE_TYPES
            or not _is_server_generated_candidate_id(candidate_id)
        ):
            raise ValueError("source package candidate parse declaration is invalid")
        detail: dict[str, object] = {
            "import_session_id": import_session.id,
            "candidate_id": candidate_id,
            "candidate_type": candidate_type,
        }
        if attribution_override:
            detail["attribution_override"] = attribution_override
        return detail
    artifact_id = declaration.get("artifact_id")
    if isinstance(artifact_id, bool):
        raise ValueError("import artifact declaration is invalid")
    try:
        parsed_artifact_id = int(artifact_id)
    except (TypeError, ValueError) as exc:
        raise ValueError("import artifact declaration is invalid") from exc
    if parsed_artifact_id <= 0:
        raise ValueError("import artifact declaration is invalid")
    detail = {
        "import_session_id": import_session.id,
        "artifact_id": parsed_artifact_id,
    }
    if attribution_override:
        detail["attribution_override"] = attribution_override
    return detail


def _validated_candidate_attribution_override(
    db: Session,
    *,
    batch: Batch,
    collector_profile_id: int | None | object,
    collection_device_id: int | None | object,
) -> dict[str, int | None]:
    override: dict[str, int | None] = {}
    if collector_profile_id is not _UNSET_ATTRIBUTION:
        if collector_profile_id is not None:
            profile = available_collector_profile(
                db, workspace_id=batch.workspace_id, profile_id=collector_profile_id
            )
            if profile is None:
                raise ValueError("collector profile is unavailable")
        override["collector_profile_id"] = collector_profile_id
    if collection_device_id is not _UNSET_ATTRIBUTION:
        if collection_device_id is not None:
            device = db.get(CollectionDevice, collection_device_id)
            if device is None or device.workspace_id != batch.workspace_id or not device.is_active:
                raise ValueError("collection device is unavailable")
        override["collection_device_id"] = collection_device_id
    return override


def _require_importable_reported_collector(
    db: Session,
    *,
    batch: Batch,
    candidate: ImportCandidate,
) -> PersonnelProfile | None:
    """Reject only source candidates with an unusable producer-reported ID."""
    if candidate.candidate_type not in SOURCE_EPISODE_CANDIDATE_TYPES:
        return None
    hint_status = str(candidate.collector_hint_status or "missing")
    reported_identifier = candidate.reported_collector_identifier
    # Preserve importability for candidates stored by the pre-normalization
    # scanner.  The marker is an absence declaration, never a real ID.
    if is_unknown_collector_identifier(reported_identifier):
        hint_status = "missing"
        reported_identifier = None
    if hint_status == "missing":
        if reported_identifier is not None:
            raise ValueError("reported collector identifier is invalid")
        return None
    if hint_status != "valid" or not isinstance(reported_identifier, str):
        raise ValueError("reported collector identifier has invalid format")
    if not _COLLECTOR_IDENTIFIER_RE.fullmatch(reported_identifier):
        raise ValueError("reported collector identifier has invalid format")
    profile = db.scalar(
        select(PersonnelProfile)
        .join(
            WorkspacePersonnelProfile,
            WorkspacePersonnelProfile.personnel_profile_id == PersonnelProfile.id,
        )
        .where(
            WorkspacePersonnelProfile.workspace_id == batch.workspace_id,
            PersonnelProfile.profile_key == reported_identifier,
        )
    )
    if profile is None:
        raise ValueError("reported collector mapping is unavailable")
    return profile


def _safe_attribution_override(value: object) -> dict[str, int | None]:
    if not isinstance(value, dict) or set(value) - {"collector_profile_id", "collection_device_id"}:
        raise ValueError("candidate attribution override is invalid")
    normalized: dict[str, int | None] = {}
    for key, raw in value.items():
        if raw is not None and (type(raw) is not int or raw <= 0):
            raise ValueError("candidate attribution override is invalid")
        normalized[key] = raw
    return normalized


def _is_server_generated_candidate_id(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 36:
        return False
    try:
        return str(UUID(value)) == value.lower()
    except (TypeError, ValueError, AttributeError):
        return False


def _object_key_from_artifact(artifact: EpisodeArtifact) -> str:
    if artifact.storage_uri.startswith("nas://"):
        object_key = artifact.storage_uri[len("nas://") :]
    elif artifact.storage_uri.startswith("oss://"):
        from data.infra import oss_client

        prefix = f"oss://{oss_client.bucket_name('raw')}/"
        if not artifact.storage_uri.startswith(prefix):
            raise ValueError("import artifact has an invalid storage URI")
        object_key = artifact.storage_uri[len(prefix) :]
    else:
        raise ValueError("import artifact has an invalid storage URI")
    if not object_key.startswith(("raw/v1/", "raw/v2/")):
        raise ValueError("import artifact has an invalid storage URI")
    return object_key


def _local_raw_path(object_key: str) -> Path:
    root = storage_root_path()
    target = (root / object_key).resolve()
    if not is_under_storage_root(target, root=root):
        raise ValueError("import raw path is outside storage root")
    return target


def import_original_storage_uri(object_key: str) -> str:
    """Return the authority URI for the same canonical raw object key."""
    if not object_key.startswith(("raw/v1/", "raw/v2/")):
        raise ValueError("import object key is outside raw namespace")
    if uses_cloud_uri_authority():
        from data.infra import oss_client

        return f"oss://{oss_client.bucket_name('raw')}/{object_key}"
    return f"nas://{object_key}"


def _publish_assembled_import(
    assembled_path: Path,
    *,
    object_key: str,
    artifact: EpisodeArtifact,
    db: Session | None = None,
    operation: ArtifactOperation | None = None,
) -> None:
    expected_uri = import_original_storage_uri(object_key)
    if artifact.storage_uri != expected_uri:
        assembled_path.unlink(missing_ok=True)
        raise ValueError("import artifact URI does not match the canonical namespace")
    if operation is not None:
        if db is None:
            raise ValueError("artifact operation database session is required")
        try:
            materialize_artifact_operation(
                db, operation_id=operation.id, source_path=assembled_path
            )
            complete_artifact_operation(db, operation_id=operation.id)
            db.commit()
        finally:
            assembled_path.unlink(missing_ok=True)
        return

    if expected_uri.startswith("oss://"):
        from data.infra import oss_client

        bucket = oss_client.bucket_name("raw")
        if oss_client.object_exists(bucket, object_key):
            assembled_path.unlink(missing_ok=True)
            raise ValueError("canonical import object already exists")
        try:
            oss_client.upload_file(assembled_path, bucket, object_key)
        finally:
            assembled_path.unlink(missing_ok=True)
        return

    destination = _local_raw_path(object_key)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        assembled_path.unlink(missing_ok=True)
        raise ValueError("canonical import object already exists")
    try:
        os.replace(assembled_path, destination)
    finally:
        assembled_path.unlink(missing_ok=True)


def _safe_child(parent: Path, name: str) -> Path:
    target = (parent / name).resolve()
    if not is_under_storage_root(target, root=parent.resolve()):
        raise ValueError("import path is outside its session root")
    return target


def _remove_exact_import_staging(import_session_id: str) -> None:
    staging = import_staging_dir(import_session_id)
    imports_root = staging.parent
    if imports_root.name != "imports" or staging.parent != imports_root.resolve():
        raise ValueError("import cleanup root is invalid")
    if staging.is_symlink():
        raise ValueError("import cleanup target is a symlink")
    if staging.exists():
        shutil.rmtree(staging)


def _server_generated_session_id(value: str) -> str:
    if not isinstance(value, str):
        raise ValueError("invalid import session id")
    try:
        return str(UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError("invalid import session id") from exc
