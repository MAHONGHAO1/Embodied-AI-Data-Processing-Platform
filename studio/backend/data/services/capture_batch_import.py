"""Generic QRDF capture source package discovery for Batch intake."""

from __future__ import annotations

import hashlib
import json
import logging
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from qrdf.exceptions import IncompatibleVersionError
from qrdf.models.episode import EpisodeMetadata
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.config import settings
from data.database import Batch, ImportCandidate, ImportSession, TaskSetSourceImport
from data.infra import oss_client
from data.integrations.qrdf.paths import validate_qrdf_external_episode_id
from data.services.capture_provenance import project_capture_provenance
from data.services.oss_import_scope import OssImportScopeError, require_oss_import_scope

CAPTURE_COMPLETE_SCHEMA = "quicdata.capture.upload-complete.v1"
CAPTURE_EPISODE_OSS_CANDIDATE_TYPE = "capture_episode_oss"
CAPTURE_EPISODE_OSS_LAYOUT_VERSION = "quicdata.capture-episode-oss.v1"
CAPTURE_SOURCE_FILE_NAMES = frozenset({"data.mcap", "metadata.json", "complete.json"})
CAPTURE_COPY_OBJECT_MAX_BYTES = 1_000_000_000
# `oss_client.multipart_copy_object()` writes the established multipart proof
# shared with the historical source path. Generic source packages use the same
# guarded data.mcap copy implementation and must verify that exact format.
_CAPTURE_MULTIPART_FORMAT = "ego-multipart-v1"
_CAPTURE_MULTIPART_FORMAT_HEADER = "x-oss-meta-quicdata-copy-format"
_CAPTURE_MULTIPART_ORIGIN_HEADER = "x-oss-meta-quicdata-copy-origin"
_MARKER_FIELDS = frozenset(
    {"schema", "schema_version", "episode_id", "ingest_mode", "objects", "completed_at"}
)
_MARKER_OBJECT_FIELDS = frozenset({"size", "etag", "crc64", "version_id"})
logger = logging.getLogger("quicdata.capture_batch_import")


class CaptureSourceIdentityConflict(ValueError):
    """The producer episode identity already names different immutable data."""


@dataclass(frozen=True)
class CaptureImportSource:
    episode_id: str
    metadata: dict[str, object]
    objects: dict[str, dict[str, object]]
    source_fingerprint: str
    # Optional source-group and device/operator hints deliberately bypass the
    # strict QRDF core model.  They are projected fail-soft and never persisted
    # as raw producer metadata.
    provenance_metadata: dict[str, object] | None = None


@dataclass(frozen=True)
class CaptureSourcePath:
    """Parsed generic package path without retaining an untrusted raw string."""

    episode_id: str
    file_name: str
    parent_key: str
    capture_date: date | None = None
    capture_hour: int | None = None


def is_capture_source_object_key(key: object) -> bool:
    """Recognize the generic package directory, including unexpected files."""
    return _capture_source_path(key, require_known_file=False) is not None


def scan_capture_episode_candidates(
    db: Session,
    *,
    batch: Batch,
    import_session: ImportSession,
    bucket: str,
    objects: Iterable[dict[str, object]],
) -> list[ImportCandidate]:
    """Discover verified generic capture packages as source Episode candidates."""
    layouts: dict[str, set[str]] = defaultdict(set)
    complete_paths: list[CaptureSourcePath] = []
    invalid_sources: set[str] = set()

    for item in objects:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        path = _capture_source_path(key, require_known_file=False)
        if path is None or not _safe_capture_id(path.episode_id):
            continue
        if path.file_name not in CAPTURE_SOURCE_FILE_NAMES:
            invalid_sources.add(path.episode_id)
            continue
        try:
            require_oss_import_scope(batch=batch, bucket=bucket, keys=[str(key)])
        except OssImportScopeError:
            invalid_sources.add(path.episode_id)
            continue
        layouts[path.episode_id].add(path.file_name)
        if path.file_name == "complete.json":
            complete_paths.append(path)

    for episode_id, files in layouts.items():
        if files != CAPTURE_SOURCE_FILE_NAMES:
            invalid_sources.add(episode_id)

    candidates: list[tuple[ImportCandidate, datetime | None]] = []
    seen_episode_ids: set[str] = set()
    for path in sorted(complete_paths, key=lambda item: item.parent_key):
        if path.episode_id in invalid_sources or path.episode_id in seen_episode_ids:
            continue
        seen_episode_ids.add(path.episode_id)
        discovered = _capture_candidate_for_complete_path(
            db,
            batch=batch,
            import_session=import_session,
            bucket=bucket,
            path=path,
        )
        if discovered is not None:
            candidates.append(discovered)

    candidates.sort(
        key=lambda item: (
            item[1] is not None,
            item[1] or datetime.min,
            item[0].original_name,
        ),
        reverse=True,
    )
    return [candidate for candidate, _captured_ended_at_value in candidates]


def scan_capture_episode_candidate(
    db: Session,
    *,
    batch: Batch,
    import_session: ImportSession,
    bucket: str,
    object_key: object,
) -> ImportCandidate | None:
    """Discover one package from a listed ``complete.json`` object.

    Date/hour listing pages intentionally do not need to contain all three
    package objects.  This helper re-HEADs the exact immutable package before
    creating its server-side candidate, so a page boundary cannot change the
    validation contract.
    """
    path = _capture_source_path(object_key, require_known_file=True)
    if path is None or path.file_name != "complete.json" or not _safe_capture_id(path.episode_id):
        return None
    discovered = _capture_candidate_for_complete_path(
        db,
        batch=batch,
        import_session=import_session,
        bucket=bucket,
        path=path,
    )
    return discovered[0] if discovered is not None else None


def _capture_candidate_for_complete_path(
    db: Session,
    *,
    batch: Batch,
    import_session: ImportSession,
    bucket: str,
    path: CaptureSourcePath,
) -> tuple[ImportCandidate, datetime | None] | None:
    source = _validate_capture_source(
        batch=batch,
        bucket=bucket,
        path=path,
        include_metadata=True,
    )
    if source is None:
        return None
    fingerprint = _source_fingerprint(bucket=bucket, source=source)
    locator = {
        "schema": CAPTURE_EPISODE_OSS_LAYOUT_VERSION,
        "bucket": bucket,
        "source": _scanner_source_payload(source),
    }
    captured_ended_at = _captured_ended_at(source.get("metadata"))
    try:
        ledger = _upsert_task_set_source_import(
            db,
            task_set_id=batch.task_set_id,
            source_fingerprint=fingerprint,
            display_name=path.episode_id,
            source_locator_json=locator,
            captured_ended_at=captured_ended_at,
        )
    except CaptureSourceIdentityConflict:
        _reject_capture_source("episode_identity_changed")
        return None
    provenance = project_capture_provenance(
        db,
        workspace_id=batch.workspace_id,
        raw_metadata=source.get("provenance_metadata"),
    )
    return (
        ImportCandidate(
            id=str(uuid4()),
            import_session_id=import_session.id,
            task_set_source_import_id=ledger.id,
            candidate_type=CAPTURE_EPISODE_OSS_CANDIDATE_TYPE,
            status="discovered",
            original_name=path.episode_id,
            size_bytes=int(source["total_size_bytes"]),
            source_fingerprint=fingerprint,
            locator_json=locator,
            source_group_key=provenance.source_group_key,
            source_group_name=provenance.source_group_name,
            source_group_status=provenance.source_group_status,
            reported_collector_identifier=provenance.collector_reported_identifier,
            collector_hint_status=provenance.collector_hint_status,
        ),
        captured_ended_at,
    )


def _capture_source_path(key: object, *, require_known_file: bool) -> CaptureSourcePath | None:
    if not isinstance(key, str) or not key or key != key.strip() or "\\" in key or "\x00" in key:
        return None
    parsed = PurePosixPath(key)
    parts = parsed.parts
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        return None
    for index in range(len(parts)):
        if parts[index : index + 3] != ("raw", "v2", "sources"):
            continue
        remaining = parts[index + 3 :]
        capture_date: date | None = None
        capture_hour: int | None = None
        if len(remaining) == 2:
            episode_id, file_name = remaining
        elif len(remaining) == 4:
            date_component, hour_component, episode_id, file_name = remaining
            if not date_component.startswith("date=") or not hour_component.startswith("hour="):
                return None
            date_text = date_component.removeprefix("date=")
            if (
                len(date_text) != 10
                or date_text[4] != "-"
                or date_text[7] != "-"
                or not (date_text[:4] + date_text[5:7] + date_text[8:]).isascii()
                or not (date_text[:4] + date_text[5:7] + date_text[8:]).isdecimal()
            ):
                return None
            try:
                capture_date = date.fromisoformat(date_text)
            except ValueError:
                return None
            hour_text = hour_component.removeprefix("hour=")
            if len(hour_text) != 2 or not hour_text.isascii() or not hour_text.isdecimal():
                return None
            capture_hour = int(hour_text)
            if not 0 <= capture_hour <= 23:
                return None
        else:
            return None
        if require_known_file and file_name not in CAPTURE_SOURCE_FILE_NAMES:
            return None
        return CaptureSourcePath(
            episode_id,
            file_name,
            PurePosixPath(*parts[: len(parts) - 1]).as_posix(),
            capture_date=capture_date,
            capture_hour=capture_hour,
        )
    return None


def _safe_capture_id(value: object) -> bool:
    try:
        validate_qrdf_external_episode_id(value)
    except ValueError:
        return False
    return True


def _validate_capture_source(
    *,
    batch: Batch,
    bucket: str,
    path: CaptureSourcePath,
    include_metadata: bool,
) -> dict[str, object] | None:
    data_key = f"{path.parent_key}/data.mcap"
    metadata_key = f"{path.parent_key}/metadata.json"
    complete_key = f"{path.parent_key}/complete.json"
    keys = [data_key, metadata_key, complete_key]
    try:
        require_oss_import_scope(batch=batch, bucket=bucket, keys=keys)
    except OssImportScopeError:
        return _reject_capture_source("source_outside_configured_scope")
    try:
        data_info = _required_object_info(bucket, data_key)
        metadata_info = _required_object_info(bucket, metadata_key)
        complete_info = _required_object_info(bucket, complete_key)
        marker = oss_client.read_json_object(bucket, complete_key, if_match=complete_info.etag)
        metadata_payload = oss_client.read_json_object(
            bucket, metadata_key, if_match=metadata_info.etag
        )
    except (OSError, ValueError, FileNotFoundError):
        return _reject_capture_source("source_object_unavailable_or_changed")

    if not _marker_matches_source(
        marker,
        path=path,
        data_info=data_info,
        metadata_info=metadata_info,
    ):
        return _reject_capture_source("marker_invalid_or_identity_mismatch")
    try:
        metadata = EpisodeMetadata.model_validate(_core_capture_metadata_payload(metadata_payload))
        metadata.check_version_compatible()
    except IncompatibleVersionError:
        return _reject_capture_source("metadata_version_incompatible")
    except (ValueError, TypeError):
        return _reject_capture_source("metadata_schema_invalid")
    if metadata.episode_id != path.episode_id:
        return _reject_capture_source("metadata_episode_id_mismatch")
    if metadata.data_file != "data.mcap":
        return _reject_capture_source("metadata_data_file_invalid")

    object_identities = {
        "data": _object_identity(key=data_key, info=data_info),
        "metadata": _object_identity(key=metadata_key, info=metadata_info),
        "complete": _object_identity(key=complete_key, info=complete_info),
    }
    if include_metadata:
        metadata_view = metadata.model_dump(mode="json", exclude_none=True)
        if not isinstance(metadata_view, dict):
            return _reject_capture_source("metadata_schema_invalid")
    else:
        metadata_view = {
            "episode_id": metadata.episode_id,
            "qrdf_version": metadata.qrdf_version,
        }
    return {
        "episode_id": path.episode_id,
        "metadata": metadata_view,
        "provenance_metadata": _capture_provenance_metadata(metadata_payload),
        "objects": object_identities,
        "total_size_bytes": sum(int(item["size_bytes"]) for item in object_identities.values()),
    }


def _marker_matches_source(
    marker: object,
    *,
    path: CaptureSourcePath,
    data_info: oss_client.OSSObjectInfo,
    metadata_info: oss_client.OSSObjectInfo,
) -> bool:
    if not isinstance(marker, dict) or set(marker) - _MARKER_FIELDS:
        return False
    schema_version = marker.get("schema_version")
    if (
        marker.get("schema") != CAPTURE_COMPLETE_SCHEMA
        or isinstance(schema_version, bool)
        or schema_version != 1
    ):
        return False
    if (
        marker.get("episode_id") != path.episode_id
        or marker.get("ingest_mode") != "trusted_offline"
    ):
        return False
    completed_at = marker.get("completed_at")
    if completed_at is not None and (not isinstance(completed_at, str) or not completed_at.strip()):
        return False
    object_specs = marker.get("objects")
    if not isinstance(object_specs, dict) or set(object_specs) != {"data.mcap", "metadata.json"}:
        return False
    return _object_matches_marker_spec(
        object_specs["data.mcap"], data_info
    ) and _object_matches_marker_spec(object_specs["metadata.json"], metadata_info)


def _object_matches_marker_spec(value: object, info: oss_client.OSSObjectInfo) -> bool:
    if not isinstance(value, dict) or set(value) - _MARKER_OBJECT_FIELDS:
        return False
    size = value.get("size")
    etag = value.get("etag")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or size <= 0
        or size != info.size
        or not isinstance(etag, str)
        or not etag
        or _normalized_etag(etag) != _normalized_etag(info.etag)
    ):
        return False
    crc64 = value.get("crc64")
    if crc64 is not None and str(crc64) != str(info.crc64 or ""):
        return False
    version_id = value.get("version_id")
    return version_id is None or str(version_id) == str(info.version_id or "")


def _required_object_info(bucket: str, key: str) -> oss_client.OSSObjectInfo:
    info = oss_client.object_info(bucket, key)
    if info is None or info.size <= 0 or not info.etag:
        raise ValueError("capture source object is unavailable")
    return info


def _object_identity(*, key: str, info: oss_client.OSSObjectInfo) -> dict[str, object]:
    return {
        "key": key,
        "size_bytes": info.size,
        "etag": info.etag,
        "crc64": info.crc64 or "",
        "version_id": info.version_id or "",
    }


def _normalized_etag(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if len(normalized) >= 2 and normalized.startswith('"') and normalized.endswith('"'):
        normalized = normalized[1:-1]
    return normalized


def _scanner_source_payload(source: dict[str, object]) -> dict[str, object]:
    metadata = source.get("metadata")
    objects = source.get("objects")
    if not isinstance(metadata, dict) or not isinstance(objects, dict):
        raise ValueError("capture source validation is incomplete")
    capture = metadata.get("capture")
    return {
        "episode_id": source["episode_id"],
        "metadata": {
            "episode_id": metadata.get("episode_id"),
            "qrdf_version": metadata.get("qrdf_version"),
            "capture_mode": capture.get("mode") if isinstance(capture, dict) else None,
            "episode_type": capture.get("episode_type") if isinstance(capture, dict) else None,
        },
        "objects": objects,
        "total_size_bytes": source["total_size_bytes"],
    }


def _source_fingerprint(*, bucket: str, source: dict[str, object]) -> str:
    payload = {
        "schema": CAPTURE_EPISODE_OSS_LAYOUT_VERSION,
        "bucket": bucket,
        "source": _scanner_source_payload(source),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _captured_ended_at(metadata: object) -> datetime | None:
    if not isinstance(metadata, dict):
        return None
    timing = metadata.get("timing")
    if not isinstance(timing, dict):
        return None
    value = timing.get("end_timestamp_ns")
    if isinstance(value, bool):
        return None
    try:
        timestamp_ns = int(value)
    except (TypeError, ValueError):
        return None
    if timestamp_ns <= 0:
        return None
    try:
        return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=timezone.utc).replace(
            tzinfo=None
        )
    except (OverflowError, OSError, ValueError):
        return None


def _upsert_task_set_source_import(
    db: Session,
    *,
    task_set_id: int,
    source_fingerprint: str,
    display_name: str,
    source_locator_json: dict[str, object],
    captured_ended_at: datetime | None,
) -> TaskSetSourceImport:
    identity_row = db.scalar(
        select(TaskSetSourceImport)
        .where(
            TaskSetSourceImport.task_set_id == task_set_id,
            TaskSetSourceImport.source_kind == "capture_oss",
            TaskSetSourceImport.display_name == display_name,
        )
        .with_for_update()
    )
    if identity_row is not None and identity_row.source_fingerprint != source_fingerprint:
        raise CaptureSourceIdentityConflict(
            "capture source episode identity already has different content"
        )
    ledger = db.scalar(
        select(TaskSetSourceImport)
        .where(
            TaskSetSourceImport.task_set_id == task_set_id,
            TaskSetSourceImport.source_fingerprint == source_fingerprint,
        )
        .with_for_update()
    )
    if ledger is None:
        inserted = TaskSetSourceImport(
            task_set_id=task_set_id,
            source_fingerprint=source_fingerprint,
            source_kind="capture_oss",
            display_name=display_name,
            source_locator_json=source_locator_json,
            status="discovered",
            captured_ended_at=captured_ended_at,
        )
        try:
            with db.begin_nested():
                db.add(inserted)
                db.flush()
        except IntegrityError:
            ledger = db.scalar(
                select(TaskSetSourceImport)
                .where(
                    TaskSetSourceImport.task_set_id == task_set_id,
                    TaskSetSourceImport.source_kind == "capture_oss",
                    TaskSetSourceImport.display_name == display_name,
                )
                .with_for_update()
            )
            if ledger is None:
                raise
            if ledger.source_fingerprint != source_fingerprint:
                raise CaptureSourceIdentityConflict(
                    "capture source episode identity already has different content"
                ) from None
        else:
            return inserted
    ledger.display_name = display_name
    ledger.source_locator_json = source_locator_json
    ledger.captured_ended_at = captured_ended_at
    db.flush()
    return ledger


def _reject_capture_source(reason: str) -> None:
    logger.warning("capture source rejected reason=%s", reason)
    return None


def load_capture_episode_import_source(
    *,
    batch: Batch,
    candidate: ImportCandidate,
) -> tuple[str, CaptureImportSource]:
    """Revalidate one generic source package before worker-side copying."""
    if candidate.candidate_type != CAPTURE_EPISODE_OSS_CANDIDATE_TYPE:
        raise ValueError("capture source candidate type is invalid")
    locator = candidate.locator_json
    if not isinstance(locator, dict) or locator.get("schema") != CAPTURE_EPISODE_OSS_LAYOUT_VERSION:
        raise ValueError("capture source candidate locator is invalid")
    bucket = locator.get("bucket")
    source = locator.get("source")
    if not isinstance(bucket, str) or not bucket.strip() or not isinstance(source, dict):
        raise ValueError("capture source candidate locator is invalid")
    episode_id = source.get("episode_id")
    raw_objects = source.get("objects")
    if not _safe_capture_id(episode_id) or not isinstance(raw_objects, dict):
        raise ValueError("capture source candidate locator is invalid")
    object_identities: dict[str, dict[str, object]] = {}
    for object_name, file_name in (
        ("data", "data.mcap"),
        ("metadata", "metadata.json"),
        ("complete", "complete.json"),
    ):
        identity = raw_objects.get(object_name)
        if not isinstance(identity, dict):
            raise ValueError("capture source candidate locator is invalid")
        key = identity.get("key")
        if not isinstance(key, str) or _capture_source_path(key, require_known_file=True) is None:
            raise ValueError("capture source candidate locator is invalid")
        path = _capture_source_path(key, require_known_file=True)
        assert path is not None
        if path.episode_id != episode_id or path.file_name != file_name:
            raise ValueError("capture source candidate locator is invalid")
        object_identities[object_name] = identity
    path = _capture_source_path(object_identities["complete"]["key"], require_known_file=True)
    assert path is not None
    current = _validate_capture_source(batch=batch, bucket=bucket, path=path, include_metadata=True)
    if current is None or not _matches_locator_source(current, object_identities):
        raise ValueError("capture source identity changed before import")
    metadata = current.get("metadata")
    if not isinstance(metadata, dict):
        raise ValueError("capture source metadata is unavailable")
    fingerprint = _source_fingerprint(bucket=bucket, source=current)
    if candidate.source_fingerprint != fingerprint:
        raise ValueError("capture source identity changed before import")
    return bucket, CaptureImportSource(
        episode_id=episode_id,
        metadata=dict(metadata),
        objects=object_identities,
        source_fingerprint=fingerprint,
        provenance_metadata=dict(current.get("provenance_metadata") or {}),
    )


def _core_capture_metadata_payload(value: object) -> object:
    """Remove untrusted additive hints before validating the QRDF core.

    Platform compatibility deliberately treats producer identity and grouping
    hints as fail-soft metadata.  A newer QRDF SDK validates those fields
    strictly for writers, so passing the entire raw document through the core
    model would incorrectly reject historical or malformed hints.
    """
    if not isinstance(value, dict):
        return value
    core = dict(value)
    for key in ("source_group", "collection_task_id", "operator"):
        core.pop(key, None)
    devices = core.get("devices")
    if isinstance(devices, list):
        normalized_devices: list[object] = []
        for device in devices:
            if not isinstance(device, dict):
                normalized_devices.append(device)
                continue
            normalized = dict(device)
            normalized.pop("serial_number", None)
            normalized.pop("os_version", None)
            # A historical producer may only include a device as an identity
            # hint.  Once its optional fields are removed it is not a QRDF
            # core device declaration and must not become an invalid empty
            # DeviceInfo object.
            if normalized:
                normalized_devices.append(normalized)
        if normalized_devices:
            core["devices"] = normalized_devices
        else:
            core.pop("devices", None)
    return core


def _capture_provenance_metadata(value: object) -> dict[str, object]:
    """Retain only the untrusted hints needed for bounded local projection."""
    if not isinstance(value, dict):
        return {}
    return {
        key: value[key]
        for key in ("source_group", "collection_task_id", "operator", "devices")
        if key in value
    }


def copy_capture_source_object(
    *,
    source_bucket: str,
    source: CaptureImportSource,
    object_name: str,
    target_key: str,
) -> bool:
    """Copy one immutable generic source object without overwriting raw data."""
    expected_file_name = {
        "data": "data.mcap",
        "metadata": "metadata.json",
        "complete": "complete.json",
    }.get(object_name)
    expected = source.objects.get(object_name)
    if expected_file_name is None or not isinstance(expected, dict):
        raise ValueError("capture source object is invalid")
    key = expected.get("key")
    if not isinstance(key, str):
        raise ValueError("capture source object is invalid")
    _validate_capture_raw_target_key(target_key, expected_file_name=expected_file_name)
    current = oss_client.object_info(source_bucket, key)
    if current is None or not _object_info_matches_identity(current, expected):
        raise ValueError("capture source identity changed during import")
    raw_bucket = oss_client.bucket_name("raw")
    size_bytes = int(expected.get("size_bytes") or 0)
    multipart = size_bytes >= CAPTURE_COPY_OBJECT_MAX_BYTES
    copy_origin = _capture_multipart_copy_origin(source_bucket=source_bucket, expected=expected)
    existing = oss_client.object_info(raw_bucket, target_key)
    if existing is not None:
        _verify_capture_raw_target(
            existing,
            expected,
            multipart=multipart,
            copy_origin=copy_origin,
        )
        return False
    try:
        if multipart:
            if object_name != "data" or not settings.source_package_oss_multipart_copy_enabled:
                raise ValueError("capture source requires enabled multipart server-side copy")
            oss_client.multipart_copy_object(
                source_bucket,
                key,
                raw_bucket,
                target_key,
                source_size=size_bytes,
                source_etag=str(expected.get("etag") or ""),
                source_version_id=str(expected.get("version_id") or "") or None,
                copy_origin=copy_origin,
            )
        else:
            oss_client.copy_object(
                source_bucket,
                key,
                raw_bucket,
                target_key,
                source_etag=str(expected.get("etag") or ""),
                source_version_id=str(expected.get("version_id") or "") or None,
                forbid_overwrite=True,
            )
    except (OSError, ValueError):
        existing = oss_client.object_info(raw_bucket, target_key)
        if existing is None:
            raise
        _verify_capture_raw_target(
            existing,
            expected,
            multipart=multipart,
            copy_origin=copy_origin,
        )
        return False
    copied = oss_client.object_info(raw_bucket, target_key)
    if copied is None:
        raise ValueError("capture raw target is unavailable after copy")
    _verify_capture_raw_target(
        copied,
        expected,
        multipart=multipart,
        copy_origin=copy_origin,
    )
    return True


def _matches_locator_source(
    current: dict[str, object],
    expected: dict[str, dict[str, object]],
) -> bool:
    objects = current.get("objects")
    if not isinstance(objects, dict):
        return False
    for name, identity in expected.items():
        actual = objects.get(name)
        identity_keys = ("key", "size_bytes", "etag", "crc64", "version_id")
        if not isinstance(actual, dict) or any(
            actual.get(key) != identity.get(key) for key in identity_keys
        ):
            return False
    return True


def _object_info_matches_identity(
    info: oss_client.OSSObjectInfo, expected: dict[str, object]
) -> bool:
    return (
        info.size == expected.get("size_bytes")
        and _normalized_etag(info.etag) == _normalized_etag(expected.get("etag"))
        and (not expected.get("crc64") or str(info.crc64 or "") == str(expected.get("crc64")))
        and (
            not expected.get("version_id")
            or str(info.version_id or "") == str(expected.get("version_id"))
        )
    )


def _verify_capture_raw_target(
    info: oss_client.OSSObjectInfo,
    expected: dict[str, object],
    *,
    multipart: bool,
    copy_origin: str,
) -> None:
    if multipart:
        metadata_proof_matches = (
            info.metadata.get(_CAPTURE_MULTIPART_FORMAT_HEADER) == _CAPTURE_MULTIPART_FORMAT
            and info.metadata.get(_CAPTURE_MULTIPART_ORIGIN_HEADER) == copy_origin
        )
        # OSS UploadPartCopy can retain source user metadata rather than the
        # metadata supplied at multipart initialization. A matching provider
        # CRC64 proves the copied bytes; size alone is never sufficient.
        crc64_proof_matches = bool(
            expected.get("crc64") and info.crc64 and str(info.crc64) == str(expected.get("crc64"))
        )
        if info.size != expected.get("size_bytes") or not (
            metadata_proof_matches or crc64_proof_matches
        ):
            raise ValueError("capture raw target identity does not match copied source")
        return
    if not _object_info_matches_identity(info, expected):
        raise ValueError("capture raw target identity does not match copied source")


def _capture_multipart_copy_origin(*, source_bucket: str, expected: dict[str, object]) -> str:
    payload = {
        "schema": "quicdata.capture-multipart-copy.v1",
        "source_bucket": source_bucket,
        "source_key": expected.get("key"),
        "source_size": expected.get("size_bytes"),
        "source_etag": expected.get("etag"),
        "source_version_id": expected.get("version_id"),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _validate_capture_raw_target_key(key: object, *, expected_file_name: str) -> None:
    if not isinstance(key, str) or not key or "\\" in key or "\x00" in key:
        raise ValueError("capture raw target is invalid")
    parsed = PurePosixPath(key)
    if (
        parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or not key.startswith(("raw/v1/", "raw/v2/"))
        or parsed.name != expected_file_name
    ):
        raise ValueError("capture raw target is invalid")
