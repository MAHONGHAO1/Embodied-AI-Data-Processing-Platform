"""Trusted legacy QuicEgo V1 candidate discovery for Batch intake.

This module deliberately knows only the historical three-file producer
layout.  It does not import legacy Task/EgoEpisode models or expose OSS source
locators outside the internal ImportCandidate JSON.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from qrdf.exceptions import IncompatibleVersionError
from qrdf.models.episode import EpisodeMetadata
from sqlalchemy import select
from sqlalchemy.orm import Session

from data.config import settings
from data.database import Batch, ImportCandidate, ImportSession, TaskSetSourceImport
from data.infra import oss_client
from data.services.capture_provenance import project_capture_provenance
from data.services.oss_import_scope import OssImportScopeError, require_oss_import_scope

EGO_EPISODE_OSS_CANDIDATE_TYPE = "ego_episode_oss"
EGO_EPISODE_OSS_LAYOUT_VERSION = "quicdata.ego-episode-oss.v1"
LEGACY_EGO_FILE_NAMES = frozenset({"data.mcap", "metadata.json", "complete.json"})
# OSS CopyObject's limit is 1 GB in decimal bytes.  Keep the direct-copy path
# strictly below that cap so a source near the provider boundary cannot enter a
# request class that OSS rejects.  At or above this threshold only data.mcap
# may use the guarded multipart-copy path below.
LEGACY_EGO_COPY_OBJECT_MAX_BYTES = 1_000_000_000
_LEGACY_EGO_ID_RE = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?\Z")
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
logger = logging.getLogger("quicdata.historical_ego_import")


@dataclass(frozen=True)
class LegacyEgoObjectPath:
    task_id: str
    device_id: str
    episode_id: str
    file_name: str
    parent_key: str


@dataclass(frozen=True)
class LegacyEgoObjectIdentity:
    """One revalidated internal source object identity."""

    key: str
    size_bytes: int
    etag: str
    crc64: str
    version_id: str


@dataclass(frozen=True)
class LegacyEgoImportSource:
    """A historical EGO episode safe for server-side raw materialization."""

    legacy_task_id: str
    legacy_device_id: str
    legacy_episode_id: str
    metadata: dict[str, object]
    objects: dict[str, LegacyEgoObjectIdentity]
    source_fingerprint: str


def is_legacy_ego_object_key(key: object) -> bool:
    """Recognize the historical directory shape, including unexpected files.

    A recognized legacy directory must never fall through to generic single-
    object import: that would bypass the completion-marker validation.
    """
    return _legacy_ego_path(key, require_known_file=False) is not None


def scan_ego_episode_candidates(
    db: Session,
    *,
    batch: Batch,
    import_session: ImportSession,
    bucket: str,
    objects: Iterable[dict[str, object]],
) -> list[ImportCandidate]:
    """Discover each verified legacy directory as one source Episode.

    ``tasks/{legacy_source_group}`` remains only an internal lineage hint.  It
    cannot aggregate a selection, name a Batch, or constrain which sources an
    ImportSession may choose.  The project ledger is keyed by the immutable
    source fingerprint, so repeated scans preserve imported/importing state.
    """
    layouts: dict[tuple[str, str, str], set[str]] = defaultdict(set)
    complete_keys: list[tuple[LegacyEgoObjectPath, str]] = []
    invalid_sources: set[tuple[str, str, str]] = set()

    for item in objects:
        if not isinstance(item, dict):
            continue
        key = item.get("key")
        path = _legacy_ego_path(key, require_known_file=False)
        if path is None:
            continue
        source_id = (path.task_id, path.device_id, path.episode_id)
        if (
            not _safe_legacy_task_group(path.task_id)
            or not _safe_legacy_id(path.device_id)
            or not _safe_legacy_id(path.episode_id)
        ):
            continue
        if path.file_name not in LEGACY_EGO_FILE_NAMES:
            invalid_sources.add(source_id)
            continue
        try:
            require_oss_import_scope(batch=batch, bucket=bucket, keys=[str(key)])
        except OssImportScopeError:
            invalid_sources.add(source_id)
            continue
        layouts[source_id].add(path.file_name)
        if path.file_name == "complete.json":
            complete_keys.append((path, str(key)))

    for source_id, files in layouts.items():
        if files != LEGACY_EGO_FILE_NAMES:
            invalid_sources.add(source_id)

    candidates: list[tuple[ImportCandidate, datetime | None]] = []
    seen_source_ids: set[tuple[str, str, str]] = set()
    for path, complete_key in sorted(complete_keys, key=lambda item: item[1]):
        source_id = (path.task_id, path.device_id, path.episode_id)
        if source_id in invalid_sources or source_id in seen_source_ids:
            continue
        seen_source_ids.add(source_id)
        source = _validate_legacy_ego_source(
            batch=batch,
            bucket=bucket,
            path=path,
            complete_key=complete_key,
            include_metadata=True,
        )
        if source is None:
            continue
        fingerprint = _source_fingerprint(bucket=bucket, task_id=path.task_id, source=source)
        locator = {
            "schema": EGO_EPISODE_OSS_LAYOUT_VERSION,
            "bucket": bucket,
            "legacy_source_group": path.task_id,
            "source": _scanner_source_payload(source),
        }
        captured_ended_at = _captured_ended_at(source.get("metadata"))
        ledger = _upsert_task_set_source_import(
            db,
            task_set_id=batch.task_set_id,
            source_fingerprint=fingerprint,
            display_name=path.episode_id,
            source_locator_json=locator,
            captured_ended_at=captured_ended_at,
        )
        provenance = project_capture_provenance(
            db,
            workspace_id=batch.workspace_id,
            raw_metadata={"collection_task_id": path.task_id},
        )
        candidates.append(
            (
                ImportCandidate(
                    id=str(uuid4()),
                    import_session_id=import_session.id,
                    task_set_source_import_id=ledger.id,
                    candidate_type=EGO_EPISODE_OSS_CANDIDATE_TYPE,
                    status="discovered",
                    original_name=path.episode_id,
                    size_bytes=int(source["total_size_bytes"]),
                    source_fingerprint=fingerprint,
                    locator_json=locator,
                    source_group_key=provenance.source_group_key,
                    source_group_name=provenance.source_group_name,
                    source_group_status=provenance.source_group_status,
                ),
                captured_ended_at,
            )
        )

    candidates.sort(
        key=lambda item: (
            item[1] is not None,
            item[1] or datetime.min,
            item[0].original_name,
        ),
        reverse=True,
    )
    return [candidate for candidate, _captured_ended_at_value in candidates]


def load_ego_episode_import_source(
    *,
    batch: Batch,
    candidate: ImportCandidate,
) -> tuple[str, LegacyEgoImportSource]:
    """Revalidate exactly one ledger-backed EGO source before copying it."""
    if candidate.candidate_type != EGO_EPISODE_OSS_CANDIDATE_TYPE:
        raise ValueError("EGO source candidate type is invalid")
    locator = candidate.locator_json
    if not isinstance(locator, dict) or locator.get("schema") != EGO_EPISODE_OSS_LAYOUT_VERSION:
        raise ValueError("EGO source candidate locator is invalid")
    bucket = _safe_bucket(locator.get("bucket"))
    legacy_source_group = locator.get("legacy_source_group")
    if not _safe_legacy_task_group(legacy_source_group):
        raise ValueError("EGO source candidate locator is invalid")
    path, expected_objects = _candidate_source_path_and_objects(
        locator.get("source"), task_id=legacy_source_group
    )
    current = _validate_legacy_ego_source(
        batch=batch,
        bucket=bucket,
        path=path,
        complete_key=expected_objects["complete"].key,
        include_metadata=True,
    )
    if current is None or not _matches_locator_source(current, expected_objects):
        raise ValueError("EGO source identity changed before import")
    metadata = current.get("metadata")
    objects = current.get("objects")
    if not isinstance(metadata, dict) or not isinstance(objects, dict):
        raise ValueError("EGO source validation is incomplete")
    identities = {
        name: _identity_from_scanned_object(objects.get(name))
        for name in ("data", "metadata", "complete")
    }
    if any(identity is None for identity in identities.values()):
        raise ValueError("EGO source validation is incomplete")
    typed_identities = {
        name: identity for name, identity in identities.items() if identity is not None
    }
    fingerprint = _source_fingerprint(bucket=bucket, task_id=legacy_source_group, source=current)
    if candidate.source_fingerprint != fingerprint:
        raise ValueError("EGO source identity changed before import")
    return (
        bucket,
        LegacyEgoImportSource(
            legacy_task_id=legacy_source_group,
            legacy_device_id=path.device_id,
            legacy_episode_id=path.episode_id,
            metadata=dict(metadata),
            objects=typed_identities,
            source_fingerprint=fingerprint,
        ),
    )


def _upsert_task_set_source_import(
    db: Session,
    *,
    task_set_id: int,
    source_fingerprint: str,
    display_name: str,
    source_locator_json: dict[str, object],
    captured_ended_at: datetime | None,
) -> TaskSetSourceImport:
    ledger = db.scalar(
        select(TaskSetSourceImport)
        .where(
            TaskSetSourceImport.task_set_id == task_set_id,
            TaskSetSourceImport.source_fingerprint == source_fingerprint,
        )
        .with_for_update()
    )
    if ledger is None:
        ledger = TaskSetSourceImport(
            task_set_id=task_set_id,
            source_fingerprint=source_fingerprint,
            source_kind="ego_oss",
            display_name=display_name,
            source_locator_json=source_locator_json,
            status="discovered",
            captured_ended_at=captured_ended_at,
        )
        db.add(ledger)
        db.flush()
        return ledger

    # A completed row must never regress to discovered because a user scanned
    # the same immutable source again.  The identity-bearing locator is still
    # refreshed from a freshly validated scan for a future authorized retry.
    ledger.display_name = display_name
    ledger.source_locator_json = source_locator_json
    ledger.captured_ended_at = captured_ended_at
    db.flush()
    return ledger


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


def copy_legacy_ego_source_object(
    *,
    source_bucket: str,
    source: LegacyEgoImportSource,
    object_name: str,
    target_key: str,
) -> bool:
    """Conditionally copy one immutable source object into Batch raw storage.

    Returns whether this invocation created the target. Existing targets are
    reusable only when their identity proves they are the same source, which
    makes retries safe without ever overwriting raw data.
    """
    expected = source.objects.get(object_name)
    expected_file_name = {
        "data": "data.mcap",
        "metadata": "metadata.json",
        "complete": "complete.json",
    }.get(object_name)
    if expected is None or expected_file_name is None:
        raise ValueError("legacy EGO source object is invalid")
    _validate_raw_target_key(target_key, expected_file_name=expected_file_name)
    current = oss_client.object_info(source_bucket, expected.key)
    if current is None or not _object_info_matches_identity(current, expected):
        raise ValueError("legacy EGO source identity changed during import")

    raw_bucket = oss_client.bucket_name("raw")
    existing = oss_client.object_info(raw_bucket, target_key)
    copy_origin = _multipart_copy_origin(source_bucket=source_bucket, identity=expected)
    if existing is not None:
        _verify_raw_target(
            existing,
            expected=expected,
            multipart=expected.size_bytes >= LEGACY_EGO_COPY_OBJECT_MAX_BYTES,
            copy_origin=copy_origin,
        )
        return False

    try:
        if expected.size_bytes >= LEGACY_EGO_COPY_OBJECT_MAX_BYTES:
            if object_name != "data" or not settings.ego_source_oss_multipart_copy_enabled:
                raise ValueError("legacy EGO source requires enabled multipart server-side copy")
            oss_client.multipart_copy_object(
                source_bucket,
                expected.key,
                raw_bucket,
                target_key,
                source_size=expected.size_bytes,
                source_etag=expected.etag,
                source_version_id=expected.version_id or None,
                copy_origin=copy_origin,
            )
        else:
            oss_client.copy_object(
                source_bucket,
                expected.key,
                raw_bucket,
                target_key,
                source_etag=expected.etag,
                source_version_id=expected.version_id or None,
                forbid_overwrite=True,
            )
    except (OSError, ValueError):
        # A concurrent worker can win the immutable create after our HEAD.
        # Reuse only a verified equivalent target; otherwise preserve the
        # failure for the fenced JobRun retry path.
        existing = oss_client.object_info(raw_bucket, target_key)
        if existing is None:
            raise
        _verify_raw_target(
            existing,
            expected=expected,
            multipart=expected.size_bytes >= LEGACY_EGO_COPY_OBJECT_MAX_BYTES,
            copy_origin=copy_origin,
        )
        return False

    copied = oss_client.object_info(raw_bucket, target_key)
    if copied is None:
        raise ValueError("legacy EGO raw target is unavailable after copy")
    try:
        _verify_raw_target(
            copied,
            expected=expected,
            multipart=expected.size_bytes >= LEGACY_EGO_COPY_OBJECT_MAX_BYTES,
            copy_origin=copy_origin,
        )
    except Exception:
        # CopyObject has returned successfully, so the immutable target was
        # created by this invocation. Do not leave it behind when the post-copy
        # identity check rejects it.
        try:
            oss_client.delete_object(raw_bucket, target_key)
        except Exception:
            logger.warning(
                "legacy EGO raw target cleanup after failed copy verification failed", exc_info=True
            )
        raise
    return True


def _legacy_ego_path(key: object, *, require_known_file: bool) -> LegacyEgoObjectPath | None:
    if not isinstance(key, str) or not key or key != key.strip() or "\\" in key or "\x00" in key:
        return None
    parsed = PurePosixPath(key)
    parts = parsed.parts
    if parsed.is_absolute() or any(part in {"", ".", ".."} for part in parts):
        return None
    for index in range(len(parts)):
        if parts[index : index + 3] != ("raw", "v1", "tasks"):
            continue
        if len(parts) != index + 9:
            return None
        task_id, devices, device_id, episodes, episode_id, file_name = parts[index + 3 :]
        if devices != "devices" or episodes != "episodes":
            return None
        if require_known_file and file_name not in LEGACY_EGO_FILE_NAMES:
            return None
        return LegacyEgoObjectPath(
            task_id=task_id,
            device_id=device_id,
            episode_id=episode_id,
            file_name=file_name,
            parent_key=PurePosixPath(*parts[: index + 8]).as_posix(),
        )
    return None


def _safe_legacy_id(value: object) -> bool:
    return isinstance(value, str) and _LEGACY_EGO_ID_RE.fullmatch(value) is not None


def _safe_legacy_task_group(value: object) -> bool:
    """Allow historical human-readable task folders without widening paths."""
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or value in {".", ".."}
        or len(value) > 128
        or len(value.encode("utf-8")) > 512
        or any(character in value for character in ("/", "\\", "\x00"))
    ):
        return False
    return not any(unicodedata.category(character).startswith("C") for character in value)


def _safe_bucket(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or not 3 <= len(value) <= 255:
        raise ValueError("legacy EGO source bucket is invalid")
    if any(character in value for character in ("/", "\\", "\x00", "\r", "\n")):
        raise ValueError("legacy EGO source bucket is invalid")
    return value


def _candidate_source_path_and_objects(
    value: object,
    *,
    task_id: str,
) -> tuple[LegacyEgoObjectPath, dict[str, LegacyEgoObjectIdentity]]:
    if not isinstance(value, dict):
        raise ValueError("legacy EGO candidate source is invalid")
    device_id = value.get("legacy_device_id")
    episode_id = value.get("legacy_episode_id")
    raw_objects = value.get("objects")
    if (
        not _safe_legacy_id(device_id)
        or not _safe_legacy_id(episode_id)
        or not isinstance(raw_objects, dict)
    ):
        raise ValueError("legacy EGO candidate source is invalid")
    expected_file_names = {
        "data": "data.mcap",
        "metadata": "metadata.json",
        "complete": "complete.json",
    }
    if set(raw_objects) != set(expected_file_names):
        raise ValueError("legacy EGO candidate source is invalid")

    identities: dict[str, LegacyEgoObjectIdentity] = {}
    source_path: LegacyEgoObjectPath | None = None
    for object_name, file_name in expected_file_names.items():
        identity = _identity_from_scanned_object(raw_objects.get(object_name))
        if identity is None:
            raise ValueError("legacy EGO candidate source is invalid")
        path = _legacy_ego_path(identity.key, require_known_file=True)
        if (
            path is None
            or path.file_name != file_name
            or path.task_id != task_id
            or path.device_id != device_id
            or path.episode_id != episode_id
        ):
            raise ValueError("legacy EGO candidate source is invalid")
        if source_path is None:
            source_path = path
        elif path.parent_key != source_path.parent_key:
            raise ValueError("legacy EGO candidate source is invalid")
        identities[object_name] = identity

    if source_path is None:
        raise ValueError("legacy EGO candidate source is invalid")
    return source_path, identities


def _identity_from_scanned_object(value: object) -> LegacyEgoObjectIdentity | None:
    if not isinstance(value, dict):
        return None
    key = value.get("key")
    size_bytes = value.get("size_bytes")
    etag = value.get("etag")
    crc64 = value.get("crc64")
    version_id = value.get("version_id")
    if (
        not isinstance(key, str)
        or not key
        or key != key.strip()
        or "\\" in key
        or "\x00" in key
        or isinstance(size_bytes, bool)
        or not isinstance(size_bytes, int)
        or size_bytes <= 0
        or not _safe_identity_text(etag, allow_empty=False)
        or not _safe_identity_text(crc64, allow_empty=True)
        or not _safe_identity_text(version_id, allow_empty=True)
    ):
        return None
    return LegacyEgoObjectIdentity(
        key=key,
        size_bytes=size_bytes,
        etag=str(etag),
        crc64=str(crc64),
        version_id=str(version_id),
    )


def _safe_identity_text(value: object, *, allow_empty: bool) -> bool:
    if not isinstance(value, str) or len(value) > 512 or value != value.strip():
        return False
    if any(character in value for character in ("\x00", "\r", "\n")):
        return False
    return allow_empty or bool(value)


def _matches_locator_source(
    current: dict[str, object],
    expected: dict[str, LegacyEgoObjectIdentity],
) -> bool:
    objects = current.get("objects")
    if not isinstance(objects, dict):
        return False
    for name, identity in expected.items():
        actual = _identity_from_scanned_object(objects.get(name))
        if actual != identity:
            return False
    return True


def _scanner_source_payload(source: dict[str, object]) -> dict[str, object]:
    metadata = source.get("metadata")
    objects = source.get("objects")
    if not isinstance(metadata, dict) or not isinstance(objects, dict):
        raise ValueError("legacy EGO source validation is incomplete")
    capture = metadata.get("capture")
    capture_type = (
        capture.get("episode_type") if isinstance(capture, dict) else metadata.get("capture_type")
    )
    return {
        "legacy_device_id": source["legacy_device_id"],
        "legacy_episode_id": source["legacy_episode_id"],
        "metadata": {
            "episode_id": metadata.get("episode_id"),
            "qrdf_version": metadata.get("qrdf_version"),
            "capture_type": capture_type,
        },
        "objects": objects,
        "total_size_bytes": source["total_size_bytes"],
    }


def _source_fingerprint(*, bucket: str, task_id: str, source: dict[str, object]) -> str:
    payload = {
        "schema": EGO_EPISODE_OSS_LAYOUT_VERSION,
        "bucket": bucket,
        "legacy_task_id": task_id,
        "source": _scanner_source_payload(source),
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _validate_raw_target_key(key: object, *, expected_file_name: str) -> None:
    if not isinstance(key, str) or not key or "\\" in key or "\x00" in key:
        raise ValueError("legacy EGO raw target is invalid")
    parsed = PurePosixPath(key)
    if (
        parsed.is_absolute()
        or any(part in {"", ".", ".."} for part in parsed.parts)
        or not key.startswith(("raw/v1/", "raw/v2/"))
        or parsed.name != expected_file_name
    ):
        raise ValueError("legacy EGO raw target is invalid")


def _object_info_matches_identity(
    info: oss_client.OSSObjectInfo,
    expected: LegacyEgoObjectIdentity,
    *,
    require_source_version: bool = True,
) -> bool:
    matches = (
        info.size == expected.size_bytes
        and _same_identity_text(info.etag, expected.etag)
        and (not expected.crc64 or _same_identity_text(info.crc64 or "", expected.crc64))
    )
    return matches and (
        not require_source_version or (info.version_id or "") == expected.version_id
    )


def _same_identity_text(left: str, right: str) -> bool:
    return _normalized_etag(left) == _normalized_etag(right)


def _normalized_etag(value: object) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    if len(normalized) >= 2 and normalized.startswith('"') and normalized.endswith('"'):
        normalized = normalized[1:-1]
    return normalized


def _verify_raw_target(
    info: oss_client.OSSObjectInfo,
    *,
    expected: LegacyEgoObjectIdentity,
    multipart: bool,
    copy_origin: str,
) -> None:
    if multipart:
        metadata_proof_matches = (
            info.metadata.get("x-oss-meta-quicdata-copy-format") == "ego-multipart-v1"
            and info.metadata.get("x-oss-meta-quicdata-copy-origin") == copy_origin
        )
        # OSS UploadPartCopy may preserve source user metadata rather than the
        # InitiateMultipartUpload metadata. A matching provider CRC64 still
        # proves the copied bytes; size alone is never sufficient.
        crc64_proof_matches = bool(
            expected.crc64 and info.crc64 and _same_identity_text(info.crc64, expected.crc64)
        )
        if info.size != expected.size_bytes or not (metadata_proof_matches or crc64_proof_matches):
            raise ValueError("legacy EGO raw target identity does not match copied source")
        return
    if not _object_info_matches_copied_target(info, expected):
        raise ValueError("legacy EGO raw target identity does not match copied source")


def _object_info_matches_copied_target(
    info: oss_client.OSSObjectInfo,
    expected: LegacyEgoObjectIdentity,
) -> bool:
    if info.size != expected.size_bytes:
        return False
    if expected.crc64:
        # OSS may re-materialize a multipart source with a different ETag
        # during cross-bucket CopyObject. The source ETag/version conditions
        # fence the copy itself; matching provider CRC64 verifies its result.
        return _same_identity_text(info.crc64 or "", expected.crc64)
    # Fail closed when no provider checksum is available.
    return _same_identity_text(info.etag, expected.etag)


def _multipart_copy_origin(*, source_bucket: str, identity: LegacyEgoObjectIdentity) -> str:
    payload = {
        "schema": "quicdata.batch.legacy-ego-copy.v1",
        "source_bucket": source_bucket,
        "source_key": identity.key,
        "source_size": identity.size_bytes,
        "source_etag": identity.etag,
        "source_version_id": identity.version_id,
    }
    encoded = json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _validate_legacy_ego_source(
    *,
    batch: Batch,
    bucket: str,
    path: LegacyEgoObjectPath,
    complete_key: str,
    include_metadata: bool = False,
) -> dict[str, object] | None:
    data_key = f"{path.parent_key}/data.mcap"
    metadata_key = f"{path.parent_key}/metadata.json"
    keys = [data_key, metadata_key, complete_key]
    try:
        require_oss_import_scope(batch=batch, bucket=bucket, keys=keys)
    except OssImportScopeError:
        return _reject_legacy_source("source_outside_configured_scope")
    try:
        data_info = _required_object_info(bucket, data_key, require_nonempty=True)
        metadata_info = _required_object_info(bucket, metadata_key, require_nonempty=True)
        complete_info = _required_object_info(bucket, complete_key, require_nonempty=True)
        marker = oss_client.read_json_object(bucket, complete_key, if_match=complete_info.etag)
        metadata_payload = oss_client.read_json_object(
            bucket, metadata_key, if_match=metadata_info.etag
        )
    except (OSError, ValueError, FileNotFoundError):
        return _reject_legacy_source("source_object_unavailable_or_changed")

    if not _marker_matches_path(marker, path):
        return _reject_legacy_source("marker_path_mismatch")
    if not _marker_matches_data_identity(
        marker,
        data_info=data_info,
        metadata_info=metadata_info,
        complete_info=complete_info,
    ):
        return _reject_legacy_source("marker_identity_mismatch")
    try:
        metadata = EpisodeMetadata.model_validate(metadata_payload)
        metadata.check_version_compatible()
    except IncompatibleVersionError:
        return _reject_legacy_source("metadata_version_incompatible")
    except (ValueError, TypeError):
        return _reject_legacy_source("metadata_schema_invalid")
    if metadata.episode_id != path.episode_id:
        return _reject_legacy_source("metadata_episode_id_mismatch")
    if metadata.data_file != "data.mcap":
        return _reject_legacy_source("metadata_data_file_invalid")
    if metadata.capture is None or metadata.capture.episode_type != "human_ego_demo":
        return _reject_legacy_source("unsupported_episode_type")

    objects = {
        "data": _object_identity(key=data_key, info=data_info),
        "metadata": _object_identity(key=metadata_key, info=metadata_info),
        "complete": _object_identity(key=complete_key, info=complete_info),
    }
    metadata_view: dict[str, object]
    if include_metadata:
        dumped = metadata.model_dump(mode="json", exclude_none=True)
        if not isinstance(dumped, dict):
            return _reject_legacy_source("metadata_schema_invalid")
        metadata_view = dumped
    else:
        metadata_view = {
            "episode_id": metadata.episode_id,
            "qrdf_version": metadata.qrdf_version,
            "capture_type": metadata.capture.episode_type,
        }
    return {
        "legacy_device_id": path.device_id,
        "legacy_episode_id": path.episode_id,
        "metadata": metadata_view,
        "objects": objects,
        "total_size_bytes": sum(int(info["size_bytes"]) for info in objects.values()),
    }


def _reject_legacy_source(reason: str) -> None:
    """Record a safe operational reason without logging source identifiers."""
    logger.warning("legacy EGO source rejected reason=%s", reason)
    return None


def _required_object_info(
    bucket: str, key: str, *, require_nonempty: bool
) -> oss_client.OSSObjectInfo:
    info = oss_client.object_info(bucket, key)
    if info is None or not info.etag or (require_nonempty and info.size <= 0):
        raise ValueError("legacy EGO source object is unavailable")
    return info


def _marker_matches_path(marker: object, path: LegacyEgoObjectPath) -> bool:
    if not isinstance(marker, dict):
        return False
    if marker.get("schema_version") != 1 or isinstance(marker.get("schema_version"), bool):
        return False
    objects = marker.get("objects")
    if (
        not isinstance(objects, list)
        or len(objects) != 2
        or not all(isinstance(item, str) for item in objects)
        or set(objects) != {"data.mcap", "metadata.json"}
    ):
        return False
    return (
        marker.get("task_id") == path.task_id
        and marker.get("device_id") == path.device_id
        and marker.get("episode_id") == path.episode_id
    )


def _marker_matches_data_identity(
    marker: dict[str, object],
    *,
    data_info: oss_client.OSSObjectInfo,
    metadata_info: oss_client.OSSObjectInfo,
    complete_info: oss_client.OSSObjectInfo,
) -> bool:
    digest = marker.get("file_digest")
    file_size = marker.get("file_size")
    if isinstance(file_size, bool):
        return False
    if digest == "" and file_size == 0:
        # Match QuicEgo's explicit pre-digest compatibility rule. A partially
        # upgraded triplet must not bypass the strict current-protocol checks.
        return not any(_metadata_sha256(info) for info in (data_info, metadata_info, complete_info))
    if (
        not isinstance(digest, str)
        or _SHA256_RE.fullmatch(digest.lower()) is None
        or not isinstance(file_size, int)
        or file_size != data_info.size
    ):
        return False
    return (
        _metadata_sha256(data_info) == digest.lower()
        and _metadata_sha256(complete_info) == digest.lower()
    )


def _metadata_sha256(info: oss_client.OSSObjectInfo) -> str:
    for key in ("x-oss-meta-sha256", "sha256"):
        raw = info.metadata.get(key)
        if isinstance(raw, str) and _SHA256_RE.fullmatch(raw.strip().lower()) is not None:
            return raw.strip().lower()
    return ""


def _object_identity(*, key: str, info: oss_client.OSSObjectInfo) -> dict[str, object]:
    return {
        "key": key,
        "size_bytes": info.size,
        "etag": info.etag,
        "crc64": info.crc64 or "",
        "version_id": info.version_id or "",
    }
