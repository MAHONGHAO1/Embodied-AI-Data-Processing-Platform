"""Direct-to-export intake for standalone native LeRobot datasets."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
from collections.abc import Mapping
from pathlib import Path, PurePosixPath
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from data.config import settings
from data.database import JobRun, Workspace
from data.infra.object_storage import (
    MultipartPart,
    ObjectStorageError,
    StorageObjectIntegrityError,
    StorageObjectNotFound,
    StorageObjectRef,
)
from data.infra.storage_provider import get_storage_provider
from data.models.catalog_dataset import CatalogDataset, CatalogDatasetVersion
from data.models.native_lerobot_direct import (
    NativeLerobotDirectObject,
    NativeLerobotDirectSource,
)
from data.services.catalog_datasets import (
    CatalogDatasetConflict,
    CatalogDatasetError,
    create_catalog_dataset,
    validate_lerobot_dataset,
)
from data.services.job_runs import (
    ManualRetryResult,
    NonRetryableJobError,
    create_or_get_job_in_transaction,
    retry_terminal_job_attempt_in_transaction,
)

_ROBOT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_ROOTS = frozenset({"data", "meta", "videos"})
_NEW_UPLOAD_ATTEMPT_FAILURE_CODES = frozenset(
    {
        "native_lerobot_direct_object_unavailable",
        "native_lerobot_direct_object_changed",
        "native_lerobot_direct_path_invalid",
        "native_lerobot_direct_manifest_invalid",
        "native_lerobot_direct_upload_incomplete",
        "native_lerobot_direct_identity_invalid",
        "native_lerobot_direct_validation_failed",
    }
)


class NativeLerobotDirectUploadError(ValueError):
    """A user-correctable direct native source declaration error."""


class NativeLerobotDirectValidationError(NonRetryableJobError):
    """A source-content failure that requires a new native upload attempt."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def create_direct_source(
    db: Session,
    *,
    name: str,
    description: str,
    robot_type: str,
    dataset_id: str,
    objects: list[dict[str, object]],
    source_workspace_id: int | None,
    created_by_user_id: int | None,
) -> NativeLerobotDirectSource:
    """Persist an export-only declaration without creating collection state."""
    display_name = str(name or "").strip()
    if not display_name or len(display_name) > 128:
        raise NativeLerobotDirectUploadError("native LeRobot name is invalid")
    normalized_robot_type = str(robot_type or "").strip()
    if not _ROBOT_TYPE_RE.fullmatch(normalized_robot_type):
        raise NativeLerobotDirectUploadError("native LeRobot robot_type is invalid")
    normalized_dataset_id = str(dataset_id or "").strip()
    if not normalized_dataset_id or len(normalized_dataset_id) > 256:
        raise NativeLerobotDirectUploadError("native LeRobot dataset_id is invalid")
    if source_workspace_id is not None and db.get(Workspace, source_workspace_id) is None:
        raise NativeLerobotDirectUploadError("source workspace does not exist")

    manifest = _normalize_manifest(objects)
    source_id = uuid4().hex
    source = NativeLerobotDirectSource(
        id=source_id,
        name=display_name,
        description=str(description or ""),
        source_workspace_id=source_workspace_id,
        created_by_user_id=created_by_user_id,
        robot_type=normalized_robot_type,
        dataset_id=normalized_dataset_id,
        status="declared",
        file_count=len(manifest),
        total_size=sum(int(item["size_bytes"]) for item in manifest),
        manifest_sha256=_manifest_sha256(
            robot_type=normalized_robot_type,
            dataset_id=normalized_dataset_id,
            objects=manifest,
        ),
    )
    db.add(source)
    for item in manifest:
        path = str(item["path"])
        source.objects.append(
            NativeLerobotDirectObject(
                id=uuid4().hex,
                path=path,
                object_key=f"export/v1/native-lerobot-direct/{source_id}/objects/{path}",
                size_bytes=int(item["size_bytes"]),
                sha256=str(item["sha256"]),
                status="declared",
            )
        )
    db.flush()
    return source


def get_direct_source(db: Session, *, source_id: str) -> NativeLerobotDirectSource:
    source = (
        db.query(NativeLerobotDirectSource)
        .options(selectinload(NativeLerobotDirectSource.objects))
        .filter(NativeLerobotDirectSource.id == source_id)
        .one_or_none()
    )
    if source is None:
        raise LookupError("native LeRobot direct source does not exist")
    return source


def cancel_direct_source_upload(db: Session, *, source_id: str) -> NativeLerobotDirectSource:
    """Cancel an unqueued direct upload and reclaim only its declared export keys.

    Multipart creation has a deliberate crash window between the provider
    request and durable ``upload_id`` persistence.  Cleanup therefore queries
    the provider for each immutable source-owned key rather than trusting the
    database upload ID alone.  A queued source is intentionally not
    cancellable: validation may already be reading its frozen export identity.
    """
    source = _locked_source(db, source_id=source_id)
    if source.status not in {"declared", "uploading", "cancelled"}:
        raise NativeLerobotDirectUploadError("native LeRobot source can no longer be cancelled")

    provider = get_storage_provider()
    for item in source.objects:
        declared_ref = _declared_ref(item)
        for upload_id in provider.list_multipart_upload_ids(declared_ref):
            provider.abort_multipart(declared_ref, upload_id)
        remaining_uploads = tuple(provider.list_multipart_upload_ids(declared_ref))
        if remaining_uploads:
            raise ObjectStorageError(
                "native LeRobot multipart cancellation retained an active upload"
            )

        # A partially completed source can contain an immutable object ref.
        # Otherwise HEAD the server-owned declared key, which also catches a
        # completion that won a race with the cancellation request.
        ref = _direct_object_ref(item) if item.provider_ref_json else declared_ref
        try:
            persisted = provider.head(ref)
        except StorageObjectNotFound:
            persisted = None
        if persisted is not None:
            if persisted.bucket_role != "export" or persisted.object_key != item.object_key:
                raise ObjectStorageError(
                    "native LeRobot cancellation received an invalid export identity"
                )
            provider.delete_exact(persisted)
            try:
                provider.head(declared_ref)
            except StorageObjectNotFound:
                pass
            else:
                raise ObjectStorageError("native LeRobot cancellation retained an export object")

        item.status = "cancelled"
        item.upload_id = ""
        item.provider_ref_json = {}

    source.status = "cancelled"
    source.error_code = ""
    source.error_message = ""
    db.flush()
    return source


def direct_source_item(source: NativeLerobotDirectSource) -> dict[str, object]:
    return {
        "id": source.id,
        "name": source.name,
        "description": source.description or "",
        "source_workspace_id": source.source_workspace_id,
        "robot_type": source.robot_type,
        "dataset_id": source.dataset_id,
        "status": source.status,
        "file_count": source.file_count,
        "total_size": source.total_size,
        "manifest_sha256": source.manifest_sha256,
        "marker_ref": source.marker_ref_json or None,
        "catalog_dataset_id": source.catalog_dataset_id,
        "catalog_dataset_version_id": source.catalog_dataset_version_id,
        "validation_job_id": source.validation_job_id,
        "error_code": source.error_code or None,
        "error_message": source.error_message or None,
        "objects": [
            {
                "id": item.id,
                "path": item.path,
                "object_key": item.object_key,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
                "status": item.status,
            }
            for item in sorted(source.objects, key=lambda value: value.path)
        ],
    }


def start_direct_object_multipart(
    db: Session, *, source_id: str, object_id: str
) -> dict[str, object]:
    """Durably initialize one provider multipart upload for a declared object."""
    source, item = _locked_object(db, source_id=source_id, object_id=object_id)
    if source.status not in {"declared", "uploading"}:
        raise NativeLerobotDirectUploadError("native LeRobot source is not uploadable")
    if item.status == "completed":
        return _direct_object_item(item)
    if item.status not in {"declared", "uploading"}:
        raise NativeLerobotDirectUploadError("native LeRobot object is not uploadable")
    if item.upload_id:
        item.status = "uploading"
        source.status = "uploading"
        db.flush()
        return _direct_object_item(item)

    # Persist intent before provider state exists. If the process dies after
    # this commit, retry uses the same declared immutable target rather than a
    # user-controlled path or a collection upload session.
    item.status = "uploading"
    source.status = "uploading"
    db.flush()
    db.commit()

    provider = get_storage_provider()
    provisional = _declared_ref(item)
    upload_id = provider.create_multipart(provisional)

    source, item = _locked_object(db, source_id=source_id, object_id=object_id)
    if source.status == "cancelled" or item.status == "cancelled":
        provider.abort_multipart(provisional, upload_id)
        raise NativeLerobotDirectUploadError("native LeRobot source was cancelled")
    if item.upload_id:
        if item.upload_id != upload_id:
            provider.abort_multipart(provisional, upload_id)
        return _direct_object_item(item)
    item.upload_id = str(upload_id)
    item.status = "uploading"
    source.status = "uploading"
    db.flush()
    return _direct_object_item(item)


def sign_direct_object_part(
    db: Session, *, source_id: str, object_id: str, part_number: int
) -> dict[str, object]:
    """Return one short-lived UploadPart URL for a server-owned export key."""
    source, item = _locked_object(db, source_id=source_id, object_id=object_id)
    if source.status == "cancelled" or item.status == "cancelled":
        raise NativeLerobotDirectUploadError("native LeRobot source was cancelled")
    if item.status != "uploading" or not item.upload_id:
        raise NativeLerobotDirectUploadError("native LeRobot multipart upload is not initialized")
    total_parts = _total_parts(item.size_bytes)
    if isinstance(part_number, bool) or not 1 <= part_number <= total_parts:
        raise NativeLerobotDirectUploadError("native LeRobot multipart part is invalid")
    url = get_storage_provider().sign_part(_declared_ref(item), item.upload_id, part_number)
    return {
        **_direct_object_item(item),
        "part_number": part_number,
        "content_length": _expected_part_size(item.size_bytes, part_number),
        "method": "PUT",
        "url": url,
        "headers": {"Content-Type": "application/octet-stream"},
    }


def complete_direct_object_multipart(
    db: Session,
    *,
    source_id: str,
    object_id: str,
    parts: list[dict[str, object]],
) -> dict[str, object]:
    """Complete only the provider-confirmed multipart manifest."""
    source, item = _locked_object(db, source_id=source_id, object_id=object_id)
    if source.status == "cancelled" or item.status == "cancelled":
        raise NativeLerobotDirectUploadError("native LeRobot source was cancelled")
    if item.status == "completed":
        return _direct_object_item(item)
    if item.status != "uploading" or not item.upload_id:
        raise NativeLerobotDirectUploadError("native LeRobot multipart upload is not initialized")
    expected_parts = _total_parts(item.size_bytes)
    requested = _normalize_parts(parts, total_parts=expected_parts)
    provider = get_storage_provider()
    declared_ref = _declared_ref(item)
    upload_id = str(item.upload_id)
    try:
        provider_parts = tuple(provider.list_parts(declared_ref, upload_id))
    except ObjectStorageError as exc:
        # CompleteMultipart can have reached the provider even if its response
        # was lost. Reconcile only the immutable key for this object; do not
        # turn a transient provider outage into a false completion.
        reconciled = _completed_object_ref_if_present(provider, declared_ref)
        if reconciled is None:
            raise exc
        return _persist_completed_direct_object(
            db,
            source_id=source_id,
            object_id=object_id,
            ref=reconciled,
            provider=provider,
        )
    if len(provider_parts) != expected_parts:
        raise NativeLerobotDirectUploadError("native LeRobot multipart upload is incomplete")
    verified = [
        {
            "part_number": int(part.number),
            "etag": str(part.etag).strip('"'),
        }
        for part in provider_parts
    ]
    if requested != verified:
        raise NativeLerobotDirectUploadError(
            "native LeRobot multipart parts do not match the provider"
        )
    if any(
        int(part.size_bytes) != _expected_part_size(item.size_bytes, int(part.number))
        for part in provider_parts
    ):
        raise NativeLerobotDirectUploadError(
            "native LeRobot multipart part size does not match declaration"
        )
    # Persist the checked provider part manifest before consuming the upload
    # ID; a lost CompleteMultipart response can subsequently be reconciled by
    # HEAD through the immutable object key.
    db.commit()
    try:
        persisted = provider.complete_multipart(
            declared_ref,
            upload_id,
            [
                MultipartPart(int(part.number), str(part.etag), int(part.size_bytes))
                for part in provider_parts
            ],
        )
    except ObjectStorageError as exc:
        reconciled = _completed_object_ref_if_present(provider, declared_ref)
        if reconciled is None:
            raise exc
        return _persist_completed_direct_object(
            db,
            source_id=source_id,
            object_id=object_id,
            ref=reconciled,
            provider=provider,
        )
    if persisted.size_bytes != item.size_bytes or not (persisted.version_id or persisted.etag):
        raise NativeLerobotDirectUploadError("native LeRobot completed object identity is invalid")
    return _persist_completed_direct_object(
        db,
        source_id=source_id,
        object_id=object_id,
        ref=persisted,
        provider=provider,
    )


def queue_direct_source_validation(
    db: Session, *, source_id: str, actor_id: int | None
) -> tuple[NativeLerobotDirectSource, object]:
    """Queue validation only after every declared export object is complete."""
    source = _locked_source(db, source_id=source_id)
    if source.status == "succeeded":
        raise NativeLerobotDirectUploadError("native LeRobot source is already validated")
    if source.status == "failed":
        raise NativeLerobotDirectUploadError(
            "native LeRobot source failed; create a new upload attempt"
        )
    if source.status not in {"declared", "uploading", "queued"}:
        raise NativeLerobotDirectUploadError("native LeRobot source cannot be validated")
    if any(item.status != "completed" or not item.provider_ref_json for item in source.objects):
        raise NativeLerobotDirectUploadError("native LeRobot upload is incomplete")
    job = create_or_get_job_in_transaction(
        db,
        kind="native_lerobot_direct_validate",
        resource_type="native_lerobot_direct_source",
        resource_id=source.id,
        idempotency_key=f"native-lerobot-direct-validate:{source.id}",
        queue="ingest",
        actor_id=actor_id,
        detail={"native_lerobot_direct_source_id": source.id},
    )
    source.status = "queued"
    source.validation_job_id = job.id
    source.error_code = ""
    source.error_message = ""
    db.flush()
    return source, job


def retry_direct_source_validation(
    db: Session,
    *,
    source_id: str,
    actor_id: int | None,
) -> ManualRetryResult:
    """Retry a system failure against the same immutable export manifest.

    Direct source content failures require a new browser upload attempt.  A
    durable infrastructure failure, on the other hand, can reuse the exact
    frozen export object identities.  The source's current-job fence is updated
    in this transaction with the manual retry job so a fresh delivery cannot
    be mistaken for its terminal predecessor.
    """
    source = _locked_source(db, source_id=source_id)
    current_job_id = str(source.validation_job_id or "")
    if not current_job_id:
        raise NativeLerobotDirectUploadError("native LeRobot source has no validation job to retry")

    current = db.get(JobRun, current_job_id)
    if not _is_current_direct_validation_job(source, current):
        raise NativeLerobotDirectUploadError("native LeRobot source validation job is invalid")

    if source.status in {"queued", "running", "failed"} and current.status not in {
        "failed",
        "cancelled",
        "succeeded",
    }:
        return ManualRetryResult(job=current, created=False)

    # Lease reclamation updates the JobRun independently from the source
    # projection. Once automatic recovery is exhausted, the fenced current
    # job is terminal while the source can still correctly read queued/running.
    # A source-level retry creates and binds the only valid replacement job.
    if source.status not in {"queued", "running", "failed"}:
        raise NativeLerobotDirectUploadError(
            "native LeRobot source validation is not eligible for retry"
        )
    if source.error_code in _NEW_UPLOAD_ATTEMPT_FAILURE_CODES:
        raise NativeLerobotDirectUploadError(
            "native LeRobot source content failed; create a new upload attempt"
        )
    if current.status not in {"failed", "cancelled"}:
        raise NativeLerobotDirectUploadError(
            "native LeRobot source validation is not eligible for retry"
        )
    try:
        result = retry_terminal_job_attempt_in_transaction(
            db,
            current.id,
            actor_id=actor_id,
        )
    except ValueError as exc:
        raise NativeLerobotDirectUploadError(
            "native LeRobot source validation is not eligible for retry"
        ) from exc

    retry = result.job
    if not _is_current_direct_validation_job(source, retry):
        raise NativeLerobotDirectUploadError("native LeRobot retry job is invalid")
    source.status = "queued"
    source.validation_job_id = retry.id
    source.error_code = ""
    source.error_message = ""
    db.flush()
    return result


def run_direct_source_validation(db: Session, job: object) -> dict[str, object]:
    """Validate one uploaded native source and freeze it into the global catalog.

    This intentionally owns no collection entities. A source workspace is
    audit provenance only; successful discovery is through the global catalog.
    """
    source_id = str(getattr(job, "resource_id", "") or "")
    if getattr(job, "resource_type", "") != "native_lerobot_direct_source" or not source_id:
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_job_invalid",
            "native LeRobot validation job has an invalid resource",
        )
    source = _locked_source(db, source_id=source_id)
    if source.validation_job_id != getattr(job, "id", None):
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_job_stale",
            "native LeRobot validation job is no longer current",
        )
    if source.status == "succeeded":
        return _validation_result(source)
    if source.status == "cancelled":
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_cancelled",
            "native LeRobot source was cancelled",
        )
    if source.status not in {"queued", "running", "failed"}:
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_state_invalid",
            "native LeRobot source is not ready for validation",
        )

    source.status = "running"
    source.error_code = ""
    source.error_message = ""
    db.flush()

    try:
        provider = get_storage_provider()
        scratch_root = _direct_scratch_root()
        with tempfile.TemporaryDirectory(
            prefix=f"native-lerobot-{source.id[:12]}-", dir=str(scratch_root)
        ) as temporary:
            root = Path(temporary)
            objects = _materialize_direct_source(source, provider=provider, root=root)
            marker_payload = _direct_marker_payload(source, objects=objects)
            marker_ref = _persist_direct_marker(
                source,
                provider=provider,
                root=root,
                payload=marker_payload,
            )
            # The export completion marker is durable evidence of this upload attempt.
            # Persist its identity before catalog materialization, whose validation can
            # fail without invalidating the already-written export source.
            source.marker_ref_json = _ref_json(marker_ref)
            db.flush()
            snapshot = {
                "schema": "quicstudio.lerobot-direct-source.v1",
                "native_lerobot_direct_source_id": source.id,
                "robot_type": source.robot_type,
                "dataset_id": source.dataset_id,
                "manifest_sha256": source.manifest_sha256,
                "objects": objects,
                "marker": {"path": "complete.json", "ref": _ref_json(marker_ref)},
            }
            dataset, version = _create_direct_catalog_version(
                db,
                source=source,
                snapshot=snapshot,
            )
            source.catalog_dataset_id = dataset.id
            source.catalog_dataset_version_id = version.id
            source.status = "succeeded"
            source.error_code = ""
            source.error_message = ""
            db.flush()
            return _validation_result(source)
    except NativeLerobotDirectValidationError as exc:
        if source.status != "cancelled":
            _mark_direct_source_failure(db, source, code=exc.code, message=str(exc))
        raise
    except CatalogDatasetError as exc:
        error = NativeLerobotDirectValidationError(
            "native_lerobot_direct_catalog_failed",
            exc.message,
        )
        _mark_direct_source_failure(db, source, code=error.code, message=str(error))
        raise error from exc
    except ObjectStorageError as exc:
        # Provider failures are retryable against the same immutable manifest.
        _mark_direct_source_failure(
            db,
            source,
            code="native_lerobot_direct_storage_unavailable",
            message=str(exc),
        )
        raise
    except Exception as exc:
        _mark_direct_source_failure(
            db,
            source,
            code="native_lerobot_direct_validation_error",
            message=str(exc),
        )
        raise


def _completed_object_ref_if_present(
    provider: object, declared_ref: StorageObjectRef
) -> StorageObjectRef | None:
    try:
        completed = provider.head(declared_ref)
    except ObjectStorageError:
        return None
    if (
        not isinstance(completed, StorageObjectRef)
        or completed.bucket_role != "export"
        or completed.object_key != declared_ref.object_key
        or completed.size_bytes != declared_ref.size_bytes
        or not (completed.version_id or completed.etag)
    ):
        raise NativeLerobotDirectUploadError("native LeRobot completed object identity is invalid")
    return completed


def _persist_completed_direct_object(
    db: Session,
    *,
    source_id: str,
    object_id: str,
    ref: StorageObjectRef,
    provider: object,
) -> dict[str, object]:
    source, item = _locked_object(db, source_id=source_id, object_id=object_id)
    if source.status == "cancelled" or item.status == "cancelled":
        # CompleteMultipart can race a cancellation after its provider call.
        # The key belongs only to this source, so delete the exact returned
        # identity before rejecting the stale completion.
        provider.delete_exact(ref)
        raise NativeLerobotDirectUploadError("native LeRobot source was cancelled")
    if item.status == "completed":
        return _direct_object_item(item)
    if ref.size_bytes != item.size_bytes or ref.object_key != item.object_key:
        raise NativeLerobotDirectUploadError(
            "native LeRobot completed object does not match its declaration"
        )
    item.provider_ref_json = _ref_json(ref)
    item.status = "completed"
    item.upload_id = ""
    db.flush()
    return _direct_object_item(item)


def _materialize_direct_source(
    source: NativeLerobotDirectSource,
    *,
    provider: object,
    root: Path,
) -> list[dict[str, object]]:
    _validate_direct_manifest(source)
    _enforce_direct_scratch_budget(root, additional_bytes=int(source.total_size))
    dataset_root = root / "dataset"
    dataset_root.mkdir()
    # ``/tmp`` resolves to ``/private/tmp`` on macOS. Compare canonical paths
    # so that a trusted temporary directory is not mistaken for an escape.
    dataset_root = dataset_root.resolve()
    materialized: list[dict[str, object]] = []
    for item in sorted(source.objects, key=lambda row: row.path):
        expected = _direct_object_ref(item)
        try:
            headed = provider.head(expected)
        except StorageObjectNotFound as exc:
            raise NativeLerobotDirectValidationError(
                "native_lerobot_direct_object_unavailable",
                "native LeRobot export object is unavailable",
            ) from exc
        except ObjectStorageError as exc:
            raise exc
        _assert_direct_identity(expected, headed)
        relative = PurePosixPath(item.path)
        destination = dataset_root / relative
        if destination.is_symlink() or not destination.resolve().is_relative_to(dataset_root):
            raise NativeLerobotDirectValidationError(
                "native_lerobot_direct_path_invalid",
                "native LeRobot object path is unsafe",
            )
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            downloaded = provider.download_file(expected, str(destination))
        except StorageObjectNotFound as exc:
            raise NativeLerobotDirectValidationError(
                "native_lerobot_direct_object_unavailable",
                "native LeRobot export object is unavailable",
            ) from exc
        except StorageObjectIntegrityError as exc:
            raise NativeLerobotDirectValidationError(
                "native_lerobot_direct_object_changed",
                "native LeRobot export object changed after upload",
            ) from exc
        except ObjectStorageError as exc:
            raise exc
        _assert_direct_identity(expected, downloaded)
        materialized.append({"path": item.path, "ref": _ref_json(downloaded)})
        _enforce_direct_scratch_budget(root)
    if not validate_lerobot_dataset(dataset_root):
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_validation_failed",
            "native LeRobot SDK validation failed",
        )
    return materialized


def _validate_direct_manifest(source: NativeLerobotDirectSource) -> None:
    objects = sorted(source.objects, key=lambda row: row.path)
    if len(objects) != int(source.file_count):
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_manifest_invalid",
            "native LeRobot manifest object count does not match",
        )
    normalized: list[dict[str, object]] = []
    for item in objects:
        path = _safe_relative_path(item.path)
        if item.status != "completed" or not item.provider_ref_json:
            raise NativeLerobotDirectValidationError(
                "native_lerobot_direct_upload_incomplete",
                "native LeRobot upload is incomplete",
            )
        normalized.append(
            {
                "path": path,
                "size_bytes": int(item.size_bytes),
                "sha256": str(item.sha256),
            }
        )
    if sum(int(item["size_bytes"]) for item in normalized) != int(source.total_size):
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_manifest_invalid",
            "native LeRobot manifest size does not match",
        )
    if (
        _manifest_sha256(
            robot_type=source.robot_type,
            dataset_id=source.dataset_id,
            objects=normalized,
        )
        != source.manifest_sha256
    ):
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_manifest_invalid",
            "native LeRobot manifest digest does not match",
        )


def _direct_object_ref(item: NativeLerobotDirectObject) -> StorageObjectRef:
    raw = item.provider_ref_json
    if not isinstance(raw, Mapping):
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_identity_invalid",
            "native LeRobot export object identity is missing",
        )
    ref = StorageObjectRef(
        bucket_role=str(raw.get("bucket_role") or ""),
        object_key=str(raw.get("object_key") or ""),
        version_id=str(raw.get("version_id") or "") or None,
        etag=str(raw.get("etag") or ""),
        size_bytes=int(raw.get("size_bytes") or -1),
        sha256=str(raw.get("sha256") or "") or None,
    )
    if (
        ref.bucket_role != "export"
        or ref.object_key != item.object_key
        or ref.size_bytes != int(item.size_bytes)
        or ref.sha256 != item.sha256
        or not (ref.version_id or ref.etag)
    ):
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_identity_invalid",
            "native LeRobot export object identity is invalid",
        )
    return ref


def _assert_direct_identity(expected: StorageObjectRef, actual: object) -> None:
    if not isinstance(actual, StorageObjectRef):
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_identity_invalid",
            "native LeRobot provider returned an invalid identity",
        )
    if (
        actual.bucket_role != expected.bucket_role
        or actual.object_key != expected.object_key
        or actual.size_bytes != expected.size_bytes
        or (expected.version_id and actual.version_id != expected.version_id)
        or (not expected.version_id and expected.etag and actual.etag != expected.etag)
        or (actual.sha256 and actual.sha256 != expected.sha256)
        or not (actual.version_id or actual.etag)
    ):
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_object_changed",
            "native LeRobot export object changed after upload",
        )


def _direct_marker_payload(
    source: NativeLerobotDirectSource, *, objects: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "schema": "quicstudio.native-lerobot-direct-complete.v1",
        "source_id": source.id,
        "robot_type": source.robot_type,
        "dataset_id": source.dataset_id,
        "file_count": source.file_count,
        "total_size": source.total_size,
        "manifest_sha256": source.manifest_sha256,
        "objects": objects,
    }


def _persist_direct_marker(
    source: NativeLerobotDirectSource,
    *,
    provider: object,
    root: Path,
    payload: dict[str, object],
) -> StorageObjectRef:
    marker_path = root / "complete.json"
    marker_path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    _enforce_direct_scratch_budget(root)
    ref = StorageObjectRef(
        "export",
        f"export/v1/native-lerobot-direct/{source.id}/complete.json",
        None,
        "",
        marker_path.stat().st_size,
        _sha256_file(marker_path),
    )
    try:
        persisted = provider.put_worker_object(ref, str(marker_path))
        _assert_direct_identity(ref, persisted)
        return persisted
    except ObjectStorageError as write_error:
        # A worker may die after a successful put but before its database
        # transaction commits. Reuse only a marker whose bytes exactly match
        # this source's deterministic frozen payload.
        try:
            existing = provider.head(ref)
            _assert_direct_identity(ref, existing)
            reconciled_path = root / "reconciled-complete.json"
            downloaded = provider.download_file(existing, str(reconciled_path))
            _assert_direct_identity(ref, downloaded)
            return downloaded
        except ObjectStorageError:
            raise write_error from None


def _create_direct_catalog_version(
    db: Session,
    *,
    source: NativeLerobotDirectSource,
    snapshot: dict[str, object],
) -> tuple[CatalogDataset, CatalogDatasetVersion]:
    dataset = _find_or_create_direct_catalog_dataset(db, source=source)
    next_version = (
        int(
            db.scalar(
                select(func.coalesce(func.max(CatalogDatasetVersion.version), 0)).where(
                    CatalogDatasetVersion.dataset_id == dataset.id
                )
            )
            or 0
        )
        + 1
    )
    version = CatalogDatasetVersion(
        dataset_id=dataset.id,
        version=next_version,
        status="active",
        created_by_user_id=source.created_by_user_id,
        source_snapshot_json=snapshot,
        source_snapshot_id=_snapshot_sha256(snapshot),
    )
    db.add(version)
    db.flush()
    return dataset, version


def _find_or_create_direct_catalog_dataset(
    db: Session, *, source: NativeLerobotDirectSource
) -> CatalogDataset:
    """Serialize direct-source versions on the global catalog dataset row."""
    normalized_name = source.name.strip().lower()
    statement = (
        select(CatalogDataset)
        .where(func.lower(func.btrim(CatalogDataset.name)) == normalized_name)
        .with_for_update()
    )
    dataset = db.scalar(statement)
    if dataset is None:
        try:
            dataset = create_catalog_dataset(
                db,
                name=source.name,
                description=source.description,
                source_kind="lerobot_direct",
                created_by_user_id=source.created_by_user_id,
            )
        except CatalogDatasetConflict:
            # A concurrent creator won the unique-name race. The nested
            # transaction in ``create_catalog_dataset`` has rolled back, so
            # lock the durable winner before allocating the next version.
            dataset = db.scalar(statement)
            if dataset is None:
                raise
    if dataset.source_kind != "lerobot_direct":
        raise CatalogDatasetError(
            "catalog_dataset_source_kind_conflict",
            "catalog dataset name belongs to a different source kind",
        )
    if dataset.status != "active":
        raise CatalogDatasetError(
            "catalog_dataset_archived",
            "catalog dataset is archived",
        )
    return dataset


def _direct_scratch_root() -> Path:
    configured = Path(str(getattr(settings, "scratch_root", ""))).expanduser()
    if not configured.is_absolute():
        configured = Path.cwd() / configured
    probe = configured.absolute()
    host_aliases = {Path("/tmp"), Path("/var")}  # nosec B108 - trusted OS path aliases
    while probe != probe.parent:
        if probe not in host_aliases and probe.is_symlink():
            raise NativeLerobotDirectValidationError(
                "native_lerobot_direct_scratch_unavailable",
                "native LeRobot scratch path contains a symlink",
            )
        probe = probe.parent
    try:
        configured.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_scratch_unavailable",
            "native LeRobot scratch directory is unavailable",
        ) from exc
    if configured.is_symlink():
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_scratch_unavailable",
            "native LeRobot scratch path contains a symlink",
        )
    _enforce_direct_scratch_budget(configured)
    return configured


def _enforce_direct_scratch_budget(root: Path, *, additional_bytes: int = 0) -> None:
    try:
        budget = int(getattr(settings, "scratch_max_bytes", 0) or 0)
    except (TypeError, ValueError):
        budget = 0
    if budget <= 0:
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_scratch_limit",
            "native LeRobot scratch budget is invalid",
        )
    used = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise NativeLerobotDirectValidationError(
                "native_lerobot_direct_scratch_unavailable",
                "native LeRobot scratch tree contains a symlink",
            )
        if path.is_file():
            used += path.stat().st_size
    if used + int(additional_bytes) > budget:
        raise NativeLerobotDirectValidationError(
            "native_lerobot_direct_scratch_limit",
            "native LeRobot source exceeds scratch budget",
        )


def _snapshot_sha256(snapshot: Mapping[str, object]) -> str:
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mark_direct_source_failure(
    db: Session,
    source: NativeLerobotDirectSource,
    *,
    code: str,
    message: str,
) -> None:
    source.status = "failed"
    source.error_code = str(code)[:64]
    source.error_message = str(message)[:1000]
    db.flush()


def _validation_result(source: NativeLerobotDirectSource) -> dict[str, object]:
    return {
        "native_lerobot_direct_source_id": source.id,
        "catalog_dataset_id": source.catalog_dataset_id,
        "catalog_dataset_version_id": source.catalog_dataset_version_id,
        "status": source.status,
    }


def _is_current_direct_validation_job(
    source: NativeLerobotDirectSource, job: object | None
) -> bool:
    return bool(
        isinstance(job, JobRun)
        and job.kind == "native_lerobot_direct_validate"
        and job.resource_type == "native_lerobot_direct_source"
        and job.resource_id == source.id
        and isinstance(job.detail_json, Mapping)
        and str(job.detail_json.get("native_lerobot_direct_source_id") or "") == source.id
    )


def _normalize_manifest(objects: list[dict[str, object]]) -> list[dict[str, object]]:
    if not isinstance(objects, list) or not objects:
        raise NativeLerobotDirectUploadError("native LeRobot objects are required")
    normalized: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    for raw in objects:
        if not isinstance(raw, dict):
            raise NativeLerobotDirectUploadError("native LeRobot object is invalid")
        path = _safe_relative_path(raw.get("path"))
        if path in seen_paths:
            raise NativeLerobotDirectUploadError("native LeRobot object paths must be unique")
        seen_paths.add(path)
        size = raw.get("size_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or size <= 0:
            raise NativeLerobotDirectUploadError("native LeRobot object size is invalid")
        sha256 = str(raw.get("sha256") or "").lower()
        if _SHA256_RE.fullmatch(sha256) is None:
            raise NativeLerobotDirectUploadError("native LeRobot object SHA-256 is invalid")
        normalized.append({"path": path, "size_bytes": size, "sha256": sha256})
    return sorted(normalized, key=lambda item: str(item["path"]))


def _safe_relative_path(value: object) -> str:
    path = str(value or "").strip()
    candidate = PurePosixPath(path)
    if (
        not path
        or candidate.is_absolute()
        or "\\" in path
        or "//" in path
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or candidate.parts[0] not in _ALLOWED_ROOTS
    ):
        raise NativeLerobotDirectUploadError("native LeRobot object path is invalid")
    return candidate.as_posix()


def _manifest_sha256(*, robot_type: str, dataset_id: str, objects: list[dict[str, object]]) -> str:
    encoded = json.dumps(
        {
            "robot_type": robot_type,
            "dataset_id": dataset_id,
            "objects": objects,
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _locked_source(db: Session, *, source_id: str) -> NativeLerobotDirectSource:
    source = db.scalar(
        select(NativeLerobotDirectSource)
        .options(selectinload(NativeLerobotDirectSource.objects))
        .where(NativeLerobotDirectSource.id == source_id)
        .with_for_update()
    )
    if source is None:
        raise LookupError("native LeRobot direct source does not exist")
    return source


def _locked_object(
    db: Session, *, source_id: str, object_id: str
) -> tuple[NativeLerobotDirectSource, NativeLerobotDirectObject]:
    source = _locked_source(db, source_id=source_id)
    item = next((item for item in source.objects if item.id == object_id), None)
    if item is None:
        raise LookupError("native LeRobot direct object does not exist")
    return source, item


def _declared_ref(item: NativeLerobotDirectObject) -> StorageObjectRef:
    return StorageObjectRef(
        "export",
        item.object_key,
        None,
        "",
        int(item.size_bytes),
        item.sha256,
    )


def _direct_object_item(item: NativeLerobotDirectObject) -> dict[str, object]:
    return {
        "id": item.id,
        "path": item.path,
        "object_key": item.object_key,
        "size_bytes": item.size_bytes,
        "sha256": item.sha256,
        "status": item.status,
        "provider_ref": item.provider_ref_json or None,
    }


def _ref_json(ref: StorageObjectRef) -> dict[str, object]:
    return {
        "bucket_role": ref.bucket_role,
        "object_key": ref.object_key,
        "version_id": ref.version_id,
        "etag": ref.etag,
        "size_bytes": ref.size_bytes,
        "sha256": ref.sha256,
    }


def _total_parts(size_bytes: int) -> int:
    from data.config import settings

    part_size = int(settings.import_direct_upload_part_bytes)
    total = (int(size_bytes) + part_size - 1) // part_size
    if not 1 <= total <= 10_000:
        raise NativeLerobotDirectUploadError("native LeRobot object exceeds multipart limits")
    return total


def _expected_part_size(total_size: int, part_number: int) -> int:
    from data.config import settings

    part_size = int(settings.import_direct_upload_part_bytes)
    start = (part_number - 1) * part_size
    return min(part_size, int(total_size) - start)


def _normalize_parts(
    parts: list[dict[str, object]], *, total_parts: int
) -> list[dict[str, object]]:
    if not isinstance(parts, list) or len(parts) != total_parts:
        raise NativeLerobotDirectUploadError("native LeRobot multipart manifest is invalid")
    normalized: list[dict[str, object]] = []
    for expected_number, item in enumerate(parts, start=1):
        if not isinstance(item, dict):
            raise NativeLerobotDirectUploadError("native LeRobot multipart manifest is invalid")
        part_number = item.get("part_number")
        etag = str(item.get("etag") or "").strip().strip('"')
        if part_number != expected_number or not etag or len(etag) > 256:
            raise NativeLerobotDirectUploadError("native LeRobot multipart manifest is invalid")
        normalized.append({"part_number": part_number, "etag": etag})
    return normalized
