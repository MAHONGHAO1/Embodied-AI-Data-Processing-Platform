"""Temporary ZIP delivery derived only from a successful platform LeRobot copy."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import shutil
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from data.config import settings
from data.database import JobRun, NativeLerobotBundle, NativeLerobotDataset, User
from data.infra import oss_client
from data.infra.oss_client import OSSObjectInfo
from data.security.signed_url import create_signed_download_token
from data.services.browser_object_access import issue_browser_download_url
from data.services.job_runs import (
    MAX_RETRIES,
    NonRetryableJobError,
    create_or_get_job_in_transaction,
    database_now,
)
from data.services.native_lerobot_datasets import (
    COMPLETE_MARKER_NAME,
    NativeLerobotMarker,
    NativeLerobotMarkerError,
    platform_native_lerobot_uri,
    read_platform_native_lerobot_marker,
)
from data.utils.formatting import format_api_datetime
from data.utils.storage_paths import is_under_storage_root, storage_root_path
from data.utils.storage_uri import parse_storage_uri

NATIVE_LEROBOT_BUNDLE_KIND = "native_lerobot_bundle"
NATIVE_LEROBOT_BUNDLE_QUEUE = "export"
_BUNDLE_SHA_HEADER = "x-oss-meta-sha256"
_BUNDLE_MARKER_SHA_HEADER = "x-oss-meta-marker-sha256"
_COPY_ORIGIN_HEADER = "x-oss-meta-quicdata-copy-origin"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_JOB_ID_RE = re.compile(r"^[0-9a-f-]{8,64}$")
_BUNDLE_FALLBACK_TTL_SECONDS = 600
_BUNDLE_FALLBACK_MAX_USES = 1
_BUNDLE_ERROR_MESSAGES = {
    "bundle_access_denied": "The platform bundle is not authorized to access its stored copy.",
    "bundle_state_invalid": "The platform bundle record is not in a valid state.",
    "bundle_target_changed": "The platform copy changed before the ZIP could be created.",
    "bundle_target_conflict": "The bundle destination already contains conflicting data.",
    "bundle_too_large": "The platform copy exceeds the configured ZIP size limit.",
    "bundle_unavailable": "The platform bundle could not be completed.",
}
logger = logging.getLogger("quicdata.native_lerobot_bundles")


class NativeLerobotBundleError(ValueError):
    """Safe request conflict for native LeRobot bundle operations."""


class NativeLerobotBundleFailure(NonRetryableJobError):
    """A deterministic bundle error that must settle the current JobRun."""

    def __init__(self, code: str):
        if code not in _BUNDLE_ERROR_MESSAGES:
            code = "bundle_state_invalid"
        self.code = code
        super().__init__(code)


class NativeLerobotBundleTransientError(RuntimeError):
    """A provider or local staging problem eligible for the durable retry budget."""


def request_native_lerobot_bundle(
    db: Session,
    *,
    dataset_id: int,
    actor_id: int | None,
) -> tuple[NativeLerobotBundle, JobRun | None, bool]:
    """Reuse or durably queue one ZIP for a copied native LeRobot directory."""
    _require_actor(db, actor_id)
    dataset = db.scalar(
        select(NativeLerobotDataset).where(NativeLerobotDataset.id == dataset_id).with_for_update()
    )
    if dataset is None:
        raise KeyError("native LeRobot dataset does not exist")
    _require_bundleable_dataset(dataset)
    _read_platform_marker_or_request_error(dataset)

    now = database_now(db)
    bundle = db.scalar(
        select(NativeLerobotBundle)
        .where(
            NativeLerobotBundle.native_lerobot_dataset_id == dataset.id,
            NativeLerobotBundle.marker_sha256 == dataset.marker_sha256,
        )
        .with_for_update()
    )
    if (
        bundle is not None
        and bundle.status == "succeeded"
        and bundle.bundle_uri
        and bundle.expires_at is not None
        and bundle.expires_at > now
        and _bundle_uri_matches(dataset, bundle.bundle_uri)
    ):
        return bundle, None, False
    if bundle is not None and bundle.status in {"queued", "running"} and bundle.job_id:
        job = db.get(JobRun, bundle.job_id)
        if job is not None and job.status not in {"failed", "cancelled", "succeeded"}:
            return bundle, job, False

    if bundle is None:
        bundle = NativeLerobotBundle(
            native_lerobot_dataset_id=dataset.id,
            marker_sha256=dataset.marker_sha256,
            status="queued",
        )
        db.add(bundle)
        db.flush()
    else:
        bundle.status = "queued"
        bundle.job_id = None
        bundle.bundle_uri = None
        bundle.expires_at = None
        bundle.error_code = ""
        bundle.error_message = ""
        bundle.finished_at = None
        db.flush()

    attempt = (
        int(
            db.scalar(
                select(func.count(JobRun.id)).where(
                    JobRun.kind == NATIVE_LEROBOT_BUNDLE_KIND,
                    JobRun.resource_type == "native_lerobot_bundle",
                    JobRun.resource_id == str(bundle.id),
                )
            )
            or 0
        )
        + 1
    )
    job = create_or_get_job_in_transaction(
        db,
        kind=NATIVE_LEROBOT_BUNDLE_KIND,
        resource_type="native_lerobot_bundle",
        resource_id=bundle.id,
        idempotency_key=(f"native-bundle:{bundle.id}:{dataset.marker_sha256}:attempt:{attempt}"),
        queue=NATIVE_LEROBOT_BUNDLE_QUEUE,
        actor_id=actor_id,
        workspace_id=dataset.workspace_id,
        task_set_id=dataset.task_set_id,
        detail={
            "dataset_id": dataset.id,
            "marker_sha256": dataset.marker_sha256,
            "file_count": dataset.file_count,
            "total_size": dataset.total_size,
        },
    )
    _validate_bundle_job(dataset, bundle, job)
    bundle.job_id = job.id
    db.flush()
    return bundle, job, True


def run_native_lerobot_bundle(
    db: Session,
    job: JobRun,
    *,
    temp_root: Path | None = None,
) -> dict[str, int]:
    """Create one ZIP solely from exact objects under the platform target root.

    The batch worker owns the JobRun lease and final JobRun transition.  This
    service updates the bundle projection in the same transaction and leaves
    the commit to ``complete_job`` / ``fail_job``.
    """
    if job.kind != NATIVE_LEROBOT_BUNDLE_KIND or job.resource_type != "native_lerobot_bundle":
        raise NativeLerobotBundleFailure("bundle_state_invalid")
    try:
        bundle_id = int(job.resource_id)
    except (TypeError, ValueError) as exc:
        raise NativeLerobotBundleFailure("bundle_state_invalid") from exc
    if bundle_id <= 0:
        raise NativeLerobotBundleFailure("bundle_state_invalid")

    bundle = db.scalar(
        select(NativeLerobotBundle).where(NativeLerobotBundle.id == bundle_id).with_for_update()
    )
    if bundle is None or bundle.job_id != job.id:
        raise NativeLerobotBundleFailure("bundle_state_invalid")
    dataset = db.scalar(
        select(NativeLerobotDataset)
        .where(NativeLerobotDataset.id == bundle.native_lerobot_dataset_id)
        .with_for_update()
    )
    if dataset is None:
        _fail_bundle(db, bundle, "bundle_state_invalid")
    if bundle.status not in {"queued", "running"}:
        _fail_bundle(db, bundle, "bundle_state_invalid")
    try:
        _require_bundleable_dataset(dataset)
    except NativeLerobotBundleError:
        _fail_bundle(db, bundle, "bundle_state_invalid")
    if bundle.marker_sha256 != dataset.marker_sha256:
        _fail_bundle(db, bundle, "bundle_target_changed")
    _validate_bundle_job(dataset, bundle, job)

    now = database_now(db)
    bundle.status = "running"
    bundle.error_code = ""
    bundle.error_message = ""
    bundle.finished_at = None
    db.flush()
    try:
        marker, bucket, target_root = _read_platform_marker_or_fail(
            db, dataset=dataset, bundle=bundle
        )
        _enforce_bundle_size_limit(db, bundle=bundle, dataset=dataset)
        result = _build_and_publish_bundle(
            db,
            dataset=dataset,
            bundle=bundle,
            marker=marker,
            bucket=bucket,
            target_root=target_root,
            job=job,
            temp_root=temp_root,
        )
    except NativeLerobotBundleFailure:
        raise
    except NativeLerobotBundleTransientError:
        _mark_transient_bundle_failure(db, bundle=bundle, job=job)
        raise
    except Exception as exc:
        _mark_transient_bundle_failure(db, bundle=bundle, job=job)
        raise NativeLerobotBundleTransientError(
            "platform bundle is temporarily unavailable"
        ) from exc

    bundle.status = "succeeded"
    bundle.bundle_uri = result["bundle_uri"]
    bundle.expires_at = now + timedelta(days=settings.native_lerobot_bundle_retention_days)
    bundle.error_code = ""
    bundle.error_message = ""
    bundle.finished_at = now
    db.flush()
    return {
        "file_count": marker.file_count,
        "total_size": marker.total_size,
        "bundle_size": int(result["bundle_size"]),
    }


def cleanup_expired_native_lerobot_bundles(
    db: Session,
    *,
    limit: int,
    now: datetime | None = None,
) -> int:
    """Expire at most ``limit`` ZIPs and delete only their exact expected key."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5_000:
        raise ValueError("bundle cleanup limit must be between 1 and 5000")
    current = now or datetime.utcnow()
    bundles = list(
        db.scalars(
            select(NativeLerobotBundle)
            .where(
                NativeLerobotBundle.status == "succeeded",
                NativeLerobotBundle.expires_at.is_not(None),
                NativeLerobotBundle.expires_at <= current,
            )
            .order_by(NativeLerobotBundle.expires_at.asc(), NativeLerobotBundle.id.asc())
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    expired = 0
    for bundle in bundles:
        dataset = db.get(NativeLerobotDataset, bundle.native_lerobot_dataset_id)
        if (
            dataset is None
            or not bundle.bundle_uri
            or not _bundle_uri_matches(dataset, bundle.bundle_uri)
        ):
            bundle.status = "expired"
            bundle.error_code = "bundle_state_invalid"
            bundle.error_message = _BUNDLE_ERROR_MESSAGES["bundle_state_invalid"]
            bundle.expires_at = current
            bundle.finished_at = current
            expired += 1
            continue
        bucket, key = parse_storage_uri(bundle.bundle_uri)
        try:
            oss_client.delete_object(bucket, key)
        except Exception as exc:
            raise NativeLerobotBundleTransientError(
                "platform bundle cleanup is temporarily unavailable"
            ) from exc
        bundle.status = "expired"
        bundle.expires_at = current
        bundle.finished_at = current
        bundle.error_code = ""
        bundle.error_message = ""
        expired += 1
    db.flush()
    return expired


def native_lerobot_bundle_item(bundle: NativeLerobotBundle) -> dict[str, object]:
    """Return a browser-safe bundle projection without its storage URI."""
    return {
        "id": bundle.id,
        "native_lerobot_dataset_id": bundle.native_lerobot_dataset_id,
        "marker_sha256": bundle.marker_sha256,
        "status": bundle.status,
        "job_id": bundle.job_id,
        "expires_at": format_api_datetime(bundle.expires_at),
        "error_code": bundle.error_code,
        "error_message": bundle.error_message,
        "download_available": bool(
            bundle.status == "succeeded"
            and bundle.bundle_uri
            and bundle.expires_at is not None
            and bundle.expires_at > datetime.utcnow()
        ),
        "created_at": format_api_datetime(bundle.created_at),
        "finished_at": format_api_datetime(bundle.finished_at),
        "updated_at": format_api_datetime(bundle.updated_at),
    }


def issue_native_lerobot_bundle_download(
    db: Session,
    *,
    dataset_id: int,
    bundle_id: int,
    actor_id: int | None,
) -> dict[str, object]:
    """Return a direct browser descriptor or metadata for the API fallback."""
    _require_actor(db, actor_id)
    dataset, bundle = _downloadable_bundle(db, dataset_id=dataset_id, bundle_id=bundle_id)
    if not _bundle_uri_matches(dataset, str(bundle.bundle_uri or "")):
        raise NativeLerobotBundleError("platform bundle is unavailable")
    bucket, key = parse_storage_uri(str(bundle.bundle_uri))
    info = _bundle_object_info(bucket, key)
    if (
        info is None
        or _SHA256_RE.fullmatch(str(info.metadata.get(_BUNDLE_SHA_HEADER) or "")) is None
        or not hmac.compare_digest(
            str(info.metadata.get(_BUNDLE_MARKER_SHA_HEADER) or ""),
            dataset.marker_sha256,
        )
    ):
        raise NativeLerobotBundleError("platform bundle is unavailable")
    access = issue_browser_download_url(
        str(bundle.bundle_uri),
        download_name=f"lerobot-{dataset.dataset_id}-{dataset.marker_sha256[:12]}.zip",
        media_type="application/zip",
    )
    if access is not None:
        return {"available": True, **access.as_payload()}
    token = create_signed_download_token(
        resource_type="native_lerobot_bundle",
        resource_id=bundle.id,
        subject=str(actor_id),
        ttl_seconds=_BUNDLE_FALLBACK_TTL_SECONDS,
        max_uses=_BUNDLE_FALLBACK_MAX_USES,
        extra={"dataset_id": dataset.id},
    )
    return {
        "available": True,
        "direct": False,
        "url": (
            f"/api/v1/native-lerobot-datasets/{dataset.id}/bundles/{bundle.id}/content?sig="
            f"{quote(token, safe='')}"
        ),
        "expires_at": (
            datetime.now(timezone.utc) + timedelta(seconds=_BUNDLE_FALLBACK_TTL_SECONDS)
        ).isoformat(),
        "media_type": "application/zip",
    }


def _require_actor(db: Session, actor_id: int | None) -> None:
    if actor_id is None or db.get(User, actor_id) is None:
        raise PermissionError("bundle actor is invalid")


def _require_bundleable_dataset(dataset: NativeLerobotDataset) -> None:
    if dataset.status != "active" or dataset.copy_status != "succeeded" or not dataset.oss_uri:
        raise NativeLerobotBundleError("platform copy is not ready for ZIP delivery")


def _downloadable_bundle(
    db: Session,
    *,
    dataset_id: int,
    bundle_id: int,
) -> tuple[NativeLerobotDataset, NativeLerobotBundle]:
    dataset = db.get(NativeLerobotDataset, dataset_id)
    bundle = db.get(NativeLerobotBundle, bundle_id)
    if bundle is None or dataset is None or bundle.native_lerobot_dataset_id != dataset.id:
        raise KeyError("native LeRobot bundle does not exist")
    current = database_now(db)
    if (
        dataset.status != "active"
        or dataset.copy_status != "succeeded"
        or bundle.status != "succeeded"
        or not bundle.bundle_uri
        or bundle.expires_at is None
        or bundle.expires_at <= current
    ):
        raise NativeLerobotBundleError("platform bundle is unavailable")
    return dataset, bundle


def _bundle_target_key(*, workspace_id: int, dataset_id: int, marker_sha256: str) -> str:
    if (
        isinstance(workspace_id, bool)
        or isinstance(dataset_id, bool)
        or not isinstance(workspace_id, int)
        or not isinstance(dataset_id, int)
        or workspace_id <= 0
        or dataset_id <= 0
        or _SHA256_RE.fullmatch(marker_sha256) is None
    ):
        raise NativeLerobotBundleError("bundle target is invalid")
    return (
        f"exports/v1/native-lerobot-bundles/workspaces/{workspace_id}/"
        f"datasets/{dataset_id}/{marker_sha256}.zip"
    )


def _bundle_uri_matches(dataset: NativeLerobotDataset, uri: str) -> bool:
    try:
        bucket, key = parse_storage_uri(uri)
        expected_key = _bundle_target_key(
            workspace_id=dataset.workspace_id,
            dataset_id=dataset.id,
            marker_sha256=dataset.marker_sha256,
        )
    except (NativeLerobotBundleError, ValueError):
        return False
    return bucket == oss_client.bucket_name("export") and key == expected_key


def _platform_location(dataset: NativeLerobotDataset) -> tuple[str, str]:
    try:
        expected_uri = platform_native_lerobot_uri(
            workspace_id=dataset.workspace_id,
            task_set_id=dataset.task_set_id,
            batch_id=dataset.batch_id,
            native_dataset_id=dataset.id if dataset.import_session_id is not None else None,
            robot_type=dataset.robot_type,
            dataset_id=dataset.dataset_id,
        )
    except NativeLerobotMarkerError as exc:
        raise NativeLerobotBundleFailure("bundle_state_invalid") from exc
    if dataset.oss_uri != expected_uri:
        raise NativeLerobotBundleFailure("bundle_target_changed")
    bucket = oss_client.bucket_name("export")
    target_root = expected_uri.removeprefix(f"oss://{bucket}/")
    if not target_root or target_root == expected_uri or not target_root.endswith("/"):
        raise NativeLerobotBundleFailure("bundle_state_invalid")
    return bucket, target_root


def _read_platform_marker_or_request_error(dataset: NativeLerobotDataset) -> NativeLerobotMarker:
    try:
        marker, _bucket, _target_root = _read_platform_marker(dataset)
    except NativeLerobotBundleFailure as exc:
        raise NativeLerobotBundleError(_BUNDLE_ERROR_MESSAGES[exc.code]) from exc
    except Exception as exc:
        raise NativeLerobotBundleError("platform copy is not available for ZIP delivery") from exc
    return marker


def _read_platform_marker_or_fail(
    db: Session,
    *,
    dataset: NativeLerobotDataset,
    bundle: NativeLerobotBundle,
) -> tuple[NativeLerobotMarker, str, str]:
    try:
        return _read_platform_marker(dataset)
    except NativeLerobotBundleFailure as exc:
        _fail_bundle(db, bundle, exc.code)
    except Exception as exc:
        if _is_access_denied(exc):
            _fail_bundle(db, bundle, "bundle_access_denied")
        raise NativeLerobotBundleTransientError(
            "platform bundle is temporarily unavailable"
        ) from exc
    raise AssertionError("_fail_bundle must raise")


def _read_platform_marker(dataset: NativeLerobotDataset) -> tuple[NativeLerobotMarker, str, str]:
    bucket, target_root = _platform_location(dataset)
    marker_key = f"{target_root}{COMPLETE_MARKER_NAME}"
    try:
        marker_info = oss_client.object_info(bucket, marker_key)
    except Exception as exc:
        if _is_access_denied(exc):
            raise NativeLerobotBundleFailure("bundle_access_denied") from exc
        raise
    if (
        marker_info is None
        or marker_info.metadata.get(_BUNDLE_SHA_HEADER) != dataset.marker_sha256
        or marker_info.metadata.get(_COPY_ORIGIN_HEADER) != dataset.marker_sha256
    ):
        raise NativeLerobotBundleFailure("bundle_target_changed")
    try:
        marker = read_platform_native_lerobot_marker(
            bucket=bucket,
            root_key=target_root,
            robot_type=dataset.robot_type,
            dataset_id=dataset.dataset_id,
        )
    except NativeLerobotMarkerError as exc:
        if exc.code == "marker_unavailable":
            raise NativeLerobotBundleTransientError(
                "platform marker is temporarily unavailable"
            ) from exc
        raise NativeLerobotBundleFailure("bundle_target_changed") from exc
    if (
        marker.bucket != bucket
        or marker.marker_key != marker_key
        or marker.oss_uri != f"oss://{bucket}/{target_root}"
        or marker.robot_type != dataset.robot_type
        or marker.dataset_id != dataset.dataset_id
        or marker.file_count != dataset.file_count
        or marker.total_size != dataset.total_size
        or marker.manifest_sha256 != dataset.manifest_sha256
        or not hmac.compare_digest(marker.marker_sha256, dataset.marker_sha256)
    ):
        raise NativeLerobotBundleFailure("bundle_target_changed")
    return marker, bucket, target_root


def _enforce_bundle_size_limit(
    db: Session,
    *,
    bundle: NativeLerobotBundle,
    dataset: NativeLerobotDataset,
) -> None:
    max_bytes = settings.native_lerobot_bundle_max_gb * 1024 * 1024 * 1024
    if dataset.total_size > max_bytes:
        _fail_bundle(db, bundle, "bundle_too_large")


def _build_and_publish_bundle(
    db: Session,
    *,
    dataset: NativeLerobotDataset,
    bundle: NativeLerobotBundle,
    marker: NativeLerobotMarker,
    bucket: str,
    target_root: str,
    job: JobRun,
    temp_root: Path | None,
) -> dict[str, object]:
    staging = _bundle_staging_dir(job.id, temp_root=temp_root)
    payload_root = staging / "payload"
    zip_path = staging / "bundle.zip"
    zip_timestamp = _zip_entry_timestamp(marker.completed_at)
    try:
        for item in marker.objects:
            key = f"{target_root}{item.path}"
            _require_platform_object(bucket=bucket, key=key, size=item.size, sha256=item.sha256)
            target = _safe_relative_file(payload_root, item.path)
            try:
                oss_client.download_object_to_file(target, bucket, key)
            except Exception as exc:
                if _is_access_denied(exc):
                    _fail_bundle(db, bundle, "bundle_access_denied")
                raise NativeLerobotBundleTransientError(
                    "platform payload is temporarily unavailable"
                ) from exc
            _verify_downloaded_file(target, size=item.size, sha256=item.sha256)
            _set_zip_entry_timestamp(target, zip_timestamp)

        marker_path = _safe_relative_file(payload_root, COMPLETE_MARKER_NAME)
        try:
            oss_client.download_object_to_file(marker_path, bucket, marker.marker_key)
        except Exception as exc:
            if _is_access_denied(exc):
                _fail_bundle(db, bundle, "bundle_access_denied")
            raise NativeLerobotBundleTransientError(
                "platform marker is temporarily unavailable"
            ) from exc
        _verify_downloaded_marker(marker_path, expected_sha256=marker.marker_sha256)
        _set_zip_entry_timestamp(marker_path, zip_timestamp)

        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for item in marker.objects:
                archive.write(
                    _safe_relative_file(payload_root, item.path),
                    arcname=f"{dataset.dataset_id}/{item.path}",
                )
            archive.write(marker_path, arcname=f"{dataset.dataset_id}/{COMPLETE_MARKER_NAME}")
        bundle_sha256 = _sha256_file(zip_path)
        bundle_key = _bundle_target_key(
            workspace_id=dataset.workspace_id,
            dataset_id=dataset.id,
            marker_sha256=dataset.marker_sha256,
        )
        metadata = {
            _BUNDLE_SHA_HEADER: bundle_sha256,
            _BUNDLE_MARKER_SHA_HEADER: dataset.marker_sha256,
        }
        _publish_immutable_bundle(
            db,
            bundle=bundle,
            zip_path=zip_path,
            bucket=oss_client.bucket_name("export"),
            key=bundle_key,
            sha256=bundle_sha256,
            marker_sha256=dataset.marker_sha256,
            metadata=metadata,
        )
        return {
            "bundle_uri": f"oss://{oss_client.bucket_name('export')}/{bundle_key}",
            "bundle_size": zip_path.stat().st_size,
        }
    finally:
        _remove_staging_dir(staging)


def _require_platform_object(*, bucket: str, key: str, size: int, sha256: str) -> OSSObjectInfo:
    try:
        info = oss_client.object_info(bucket, key)
    except Exception as exc:
        if _is_access_denied(exc):
            raise NativeLerobotBundleFailure("bundle_access_denied") from exc
        raise NativeLerobotBundleTransientError(
            "platform payload is temporarily unavailable"
        ) from exc
    if (
        info is None
        or info.size != size
        or info.metadata.get(_BUNDLE_SHA_HEADER) != sha256
        or info.metadata.get(_COPY_ORIGIN_HEADER) != sha256
    ):
        raise NativeLerobotBundleFailure("bundle_target_changed")
    return info


def _publish_immutable_bundle(
    db: Session,
    *,
    bundle: NativeLerobotBundle,
    zip_path: Path,
    bucket: str,
    key: str,
    sha256: str,
    marker_sha256: str,
    metadata: dict[str, str],
) -> None:
    existing = _bundle_object_info(bucket, key)
    if existing is not None:
        if _bundle_object_matches(
            existing, size=zip_path.stat().st_size, sha256=sha256, marker_sha256=marker_sha256
        ):
            return
        _fail_bundle(db, bundle, "bundle_target_conflict")
    try:
        oss_client.upload_file(
            zip_path,
            bucket,
            key,
            content_type="application/zip",
            forbid_overwrite=True,
            metadata=metadata,
        )
    except FileExistsError:
        existing = _bundle_object_info(bucket, key)
        if existing is not None and _bundle_object_matches(
            existing,
            size=zip_path.stat().st_size,
            sha256=sha256,
            marker_sha256=marker_sha256,
        ):
            return
        _fail_bundle(db, bundle, "bundle_target_conflict")
    except Exception as exc:
        if _is_access_denied(exc):
            _fail_bundle(db, bundle, "bundle_access_denied")
        raise NativeLerobotBundleTransientError(
            "platform bundle upload is temporarily unavailable"
        ) from exc


def _bundle_object_info(bucket: str, key: str) -> OSSObjectInfo | None:
    try:
        return oss_client.object_info(bucket, key)
    except Exception as exc:
        if _is_access_denied(exc):
            raise NativeLerobotBundleFailure("bundle_access_denied") from exc
        raise NativeLerobotBundleTransientError(
            "platform bundle target is temporarily unavailable"
        ) from exc


def _bundle_object_matches(
    info: OSSObjectInfo,
    *,
    size: int,
    sha256: str,
    marker_sha256: str,
) -> bool:
    return (
        info.size == size
        and hmac.compare_digest(str(info.metadata.get(_BUNDLE_SHA_HEADER) or ""), sha256)
        and hmac.compare_digest(
            str(info.metadata.get(_BUNDLE_MARKER_SHA_HEADER) or ""), marker_sha256
        )
    )


def _verify_downloaded_file(path: Path, *, size: int, sha256: str) -> None:
    if not path.is_file() or path.is_symlink() or path.stat().st_size != size:
        raise NativeLerobotBundleFailure("bundle_target_changed")
    if not hmac.compare_digest(_sha256_file(path), sha256):
        raise NativeLerobotBundleFailure("bundle_target_changed")


def _verify_downloaded_marker(path: Path, *, expected_sha256: str) -> None:
    if not path.is_file() or path.is_symlink():
        raise NativeLerobotBundleFailure("bundle_target_changed")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise NativeLerobotBundleFailure("bundle_target_changed") from exc
    actual_sha256 = hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
            "utf-8"
        )
    ).hexdigest()
    if not hmac.compare_digest(actual_sha256, expected_sha256):
        raise NativeLerobotBundleFailure("bundle_target_changed")


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while block := source.read(1024 * 1024):
            digest.update(block)
    return digest.hexdigest()


def _zip_entry_timestamp(completed_at: datetime) -> int:
    """Return a stable ZIP-safe UTC timestamp for one marker snapshot."""
    moment = completed_at
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    else:
        moment = moment.astimezone(timezone.utc)
    minimum = datetime(1980, 1, 1, tzinfo=timezone.utc)
    return int(max(moment, minimum).timestamp())


def _set_zip_entry_timestamp(path: Path, timestamp: int) -> None:
    if path.is_symlink():
        raise NativeLerobotBundleFailure("bundle_state_invalid")
    try:
        os.utime(path, (timestamp, timestamp), follow_symlinks=False)
    except OSError as exc:
        raise NativeLerobotBundleTransientError(
            "platform bundle staging is temporarily unavailable"
        ) from exc


def _bundle_staging_dir(job_id: str, *, temp_root: Path | None) -> Path:
    if _JOB_ID_RE.fullmatch(str(job_id or "")) is None:
        raise NativeLerobotBundleFailure("bundle_state_invalid")
    root = Path(temp_root) if temp_root is not None else storage_root_path()
    root = root.resolve()
    storage_root = storage_root_path()
    if not is_under_storage_root(root, root=storage_root):
        raise NativeLerobotBundleFailure("bundle_state_invalid")
    current = root
    for part in ("tmp", "native-lerobot-bundles", str(job_id)):
        current = current / part
        if current.exists():
            if current.is_symlink() or not current.is_dir():
                raise NativeLerobotBundleFailure("bundle_state_invalid")
        else:
            current.mkdir(mode=0o700)
    if not is_under_storage_root(current, root=storage_root):
        raise NativeLerobotBundleFailure("bundle_state_invalid")
    return current


def _safe_relative_file(root: Path, relative_path: str) -> Path:
    if (
        not isinstance(relative_path, str)
        or not relative_path
        or relative_path.startswith("/")
        or "\\" in relative_path
        or "\x00" in relative_path
        or any(part in {"", ".", ".."} for part in relative_path.split("/"))
    ):
        raise NativeLerobotBundleFailure("bundle_state_invalid")
    target = root.joinpath(*relative_path.split("/"))
    root_resolved = root.resolve()
    target_parent = target.parent.resolve()
    if not is_under_storage_root(target_parent, root=root_resolved):
        raise NativeLerobotBundleFailure("bundle_state_invalid")
    return target


def _remove_staging_dir(path: Path) -> None:
    try:
        if path.exists() and not path.is_symlink():
            shutil.rmtree(path)
    except Exception as exc:
        # Temporary cleanup is intentionally best-effort.  A bounded retention
        # job never follows a staging symlink and later jobs use unique IDs.
        logger.warning(
            "native_lerobot_bundle_staging_cleanup_failed error_type=%s", type(exc).__name__
        )


def _validate_bundle_job(
    dataset: NativeLerobotDataset, bundle: NativeLerobotBundle, job: JobRun
) -> None:
    if (
        job.kind != NATIVE_LEROBOT_BUNDLE_KIND
        or job.resource_type != "native_lerobot_bundle"
        or job.resource_id != str(bundle.id)
        or job.workspace_id != dataset.workspace_id
        or job.task_set_id != dataset.task_set_id
        or job.queue != NATIVE_LEROBOT_BUNDLE_QUEUE
        or str((job.detail_json or {}).get("marker_sha256") or "") != dataset.marker_sha256
    ):
        raise NativeLerobotBundleFailure("bundle_state_invalid")


def _fail_bundle(db: Session, bundle: NativeLerobotBundle, code: str) -> None:
    if code not in _BUNDLE_ERROR_MESSAGES:
        code = "bundle_state_invalid"
    bundle.status = "failed"
    bundle.error_code = code
    bundle.error_message = _BUNDLE_ERROR_MESSAGES[code]
    bundle.finished_at = database_now(db)
    db.flush()
    raise NativeLerobotBundleFailure(code)


def _mark_transient_bundle_failure(
    db: Session, *, bundle: NativeLerobotBundle, job: JobRun
) -> None:
    if int(job.retry_count or 0) >= MAX_RETRIES:
        bundle.status = "failed"
        bundle.error_code = "bundle_unavailable"
        bundle.error_message = _BUNDLE_ERROR_MESSAGES["bundle_unavailable"]
        bundle.finished_at = database_now(db)
    else:
        bundle.status = "queued"
        bundle.error_code = ""
        bundle.error_message = ""
        bundle.finished_at = None
    db.flush()


def _is_access_denied(exc: Exception) -> bool:
    return getattr(exc, "status", None) in {401, 403} or str(getattr(exc, "code", "")) in {
        "AccessDenied",
        "NoSuchAccessKeyId",
        "SignatureDoesNotMatch",
    }
