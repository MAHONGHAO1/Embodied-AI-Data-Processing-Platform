"""Safe discovery and registration of native LeRobot OSS dataset roots.

This module deliberately reads only directory prefixes, a dataset's
``complete.json`` marker, and that marker's HEAD metadata.  It never walks or
copies payload objects under ``data/``, ``meta/``, or ``videos/``.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import PurePosixPath

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from data.database import Batch, ExternalOssImportScope, JobRun, NativeLerobotDataset, TaskSet, User
from data.infra import oss_client
from data.infra.redis_client import redis_service
from data.services.batches import create_batch
from data.services.job_runs import create_or_get_job_in_transaction
from data.utils.formatting import format_api_datetime

LEROBOT_ROOT_PREFIX = "prod/raw/robot"
COMPLETE_MARKER_NAME = "complete.json"
COMPLETE_SCHEMA_VERSION = 1
MARKER_MAX_BYTES = 1024 * 1024
MAX_ROBOT_TYPES = 200
MAX_DATASETS_PER_ROBOT = 500
MAX_DISCOVERED_DATASETS = 1_000
CANDIDATE_TOKEN_TTL_SECONDS = 10 * 60
_CANDIDATE_REFERENCE_PREFIX = "quicdata:native-lerobot:candidate:"
MAX_PERSISTED_TOTAL_SIZE = (2**63) - 1
_ROBOT_TYPE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CANDIDATE_REFERENCE_RE = re.compile(r"^[A-Za-z0-9_-]{32,128}$")
_COMPLETED_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
_REQUIRED_MARKER_KEYS = frozenset(
    {
        "schema_version",
        "robot_type",
        "dataset_id",
        "objects",
        "file_count",
        "total_size",
        "manifest_sha256",
        "completed_at",
    }
)
_ALLOWED_OBJECT_ROOTS = ("data/", "meta/", "videos/")


class NativeLerobotError(ValueError):
    code = "native_lerobot_invalid"


class NativeLerobotMarkerError(NativeLerobotError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class NativeLerobotCandidateTokenError(NativeLerobotError):
    code = "native_lerobot_candidate_invalid"


class NativeLerobotDuplicateError(NativeLerobotError):
    code = "native_lerobot_already_registered"

    def __init__(self, existing_id: int):
        super().__init__(self.code)
        self.existing_id = existing_id


@dataclass(frozen=True)
class NativeLerobotObject:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class NativeLerobotMarker:
    bucket: str
    marker_key: str
    oss_uri: str
    robot_type: str
    dataset_id: str
    file_count: int
    total_size: int
    completed_at: datetime
    manifest_sha256: str
    marker_sha256: str
    objects: tuple[NativeLerobotObject, ...]


@dataclass(frozen=True)
class NativeLerobotCandidate:
    token: str
    robot_type: str
    dataset_id: str
    file_count: int
    total_size: int
    completed_at: datetime
    oss_uri: str
    manifest_sha256: str
    marker_sha256: str


def discover_native_lerobot_candidates(
    db: Session,
    *,
    workspace_id: int,
    task_set_id: int,
    actor_id: int,
) -> list[NativeLerobotCandidate]:
    """Discover bounded complete markers from enabled, exact OSS scopes only."""
    markers = discover_native_lerobot_markers(
        db,
        workspace_id=workspace_id,
        task_set_id=task_set_id,
    )
    candidates: list[NativeLerobotCandidate] = []
    for scope, marker in markers:
        candidates.append(
            NativeLerobotCandidate(
                token=_encode_candidate_token(
                    workspace_id=workspace_id,
                    task_set_id=task_set_id,
                    actor_id=actor_id,
                    scope=scope,
                    marker=marker,
                ),
                robot_type=marker.robot_type,
                dataset_id=marker.dataset_id,
                file_count=marker.file_count,
                total_size=marker.total_size,
                completed_at=marker.completed_at,
                oss_uri=marker.oss_uri,
                manifest_sha256=marker.manifest_sha256,
                marker_sha256=marker.marker_sha256,
            )
        )
    return candidates


def discover_native_lerobot_markers(
    db: Session,
    *,
    workspace_id: int,
    task_set_id: int,
    on_invalid: Callable[[ExternalOssImportScope, str, str], None] | None = None,
) -> list[tuple[ExternalOssImportScope, NativeLerobotMarker]]:
    """Discover validated markers without creating client bearer tokens."""
    _require_task_set_scope(db, workspace_id=workspace_id, task_set_id=task_set_id)
    scopes = (
        db.query(ExternalOssImportScope)
        .filter(
            ExternalOssImportScope.workspace_id == workspace_id,
            ExternalOssImportScope.task_set_id == task_set_id,
            ExternalOssImportScope.is_enabled.is_(True),
        )
        .order_by(ExternalOssImportScope.id.asc())
        .all()
    )
    markers: dict[str, tuple[ExternalOssImportScope, NativeLerobotMarker]] = {}
    for scope in scopes:
        for marker_key in _scope_marker_keys(scope):
            try:
                marker = read_native_lerobot_marker(bucket=scope.bucket, marker_key=marker_key)
            except NativeLerobotMarkerError as exc:
                # An invalid or half-written marker is not a candidate.  It
                # must not block other completed datasets in the same scope.
                if on_invalid is not None:
                    on_invalid(scope, marker_key, exc.code)
                continue
            if marker.oss_uri in markers:
                continue
            markers[marker.oss_uri] = (scope, marker)
            if len(markers) > MAX_DISCOVERED_DATASETS:
                raise NativeLerobotMarkerError("candidate_scan_limit")
    return sorted(
        markers.values(), key=lambda item: (item[1].completed_at, item[1].oss_uri), reverse=True
    )


def read_native_lerobot_marker(*, bucket: str, marker_key: str) -> NativeLerobotMarker:
    """Read and validate one marker without touching LeRobot payload objects."""
    robot_type, dataset_id, root_key = _identity_from_marker_key(marker_key)
    _validate_bucket(bucket)
    try:
        info = oss_client.object_info(bucket, marker_key)
    except Exception as exc:
        raise NativeLerobotMarkerError("marker_unavailable") from exc
    if info is None:
        raise NativeLerobotMarkerError("marker_missing")
    if info.size > MARKER_MAX_BYTES:
        raise NativeLerobotMarkerError("marker_too_large")
    expected_marker_sha256 = _metadata_sha256(info.metadata)
    if expected_marker_sha256 is None:
        raise NativeLerobotMarkerError("marker_metadata_invalid")
    try:
        payload = oss_client.read_json_object(
            bucket,
            marker_key,
            max_bytes=MARKER_MAX_BYTES,
            if_match=info.etag or None,
        )
    except Exception as exc:
        raise NativeLerobotMarkerError("marker_unavailable") from exc
    actual_marker_sha256 = hashlib.sha256(_canonical_json(payload)).hexdigest()
    if not hmac.compare_digest(expected_marker_sha256, actual_marker_sha256):
        raise NativeLerobotMarkerError("marker_metadata_invalid")
    return _validated_marker(
        bucket=bucket,
        marker_key=marker_key,
        root_key=root_key,
        expected_robot_type=robot_type,
        expected_dataset_id=dataset_id,
        payload=payload,
        marker_sha256=actual_marker_sha256,
    )


def read_platform_native_lerobot_marker(
    *,
    bucket: str,
    root_key: str,
    robot_type: str,
    dataset_id: str,
) -> NativeLerobotMarker:
    """Validate a copied marker at one platform-owned LeRobot root.

    Unlike discovery, this helper never derives a root from an external OSS
    prefix.  Callers supply a deterministic platform root and the persisted
    identity they expect there, so bundle workers can revalidate a completed
    copy without revisiting its external source authorization.
    """
    normalized_root = _strict_prefix(root_key.rstrip("/"))
    if normalized_root is None:
        raise NativeLerobotMarkerError("marker_path_invalid")
    if _ROBOT_TYPE_RE.fullmatch(robot_type) is None or not _safe_dataset_id(dataset_id):
        raise NativeLerobotMarkerError("marker_identity_invalid")
    _validate_bucket(bucket)
    marker_key = f"{normalized_root}/{COMPLETE_MARKER_NAME}"
    try:
        info = oss_client.object_info(bucket, marker_key)
    except Exception as exc:
        raise NativeLerobotMarkerError("marker_unavailable") from exc
    if info is None:
        raise NativeLerobotMarkerError("marker_missing")
    if info.size > MARKER_MAX_BYTES:
        raise NativeLerobotMarkerError("marker_too_large")
    expected_marker_sha256 = _metadata_sha256(info.metadata)
    if expected_marker_sha256 is None:
        raise NativeLerobotMarkerError("marker_metadata_invalid")
    try:
        payload = oss_client.read_json_object(
            bucket,
            marker_key,
            max_bytes=MARKER_MAX_BYTES,
            if_match=info.etag or None,
        )
    except Exception as exc:
        raise NativeLerobotMarkerError("marker_unavailable") from exc
    actual_marker_sha256 = hashlib.sha256(_canonical_json(payload)).hexdigest()
    if not hmac.compare_digest(expected_marker_sha256, actual_marker_sha256):
        raise NativeLerobotMarkerError("marker_metadata_invalid")
    return _validated_marker(
        bucket=bucket,
        marker_key=marker_key,
        root_key=normalized_root,
        expected_robot_type=robot_type,
        expected_dataset_id=dataset_id,
        payload=payload,
        marker_sha256=actual_marker_sha256,
    )


def register_native_lerobot_dataset(
    db: Session,
    *,
    workspace_id: int,
    task_set_id: int,
    name: str,
    candidate_token: str,
    actor_id: int | None,
) -> tuple[Batch, NativeLerobotDataset, JobRun]:
    """Revalidate one opaque candidate and atomically queue its platform copy."""
    payload = _decode_candidate_token(candidate_token, actor_id=actor_id)
    if payload["workspace_id"] != workspace_id or payload["task_set_id"] != task_set_id:
        raise NativeLerobotCandidateTokenError("candidate scope is invalid")
    _require_task_set_scope(db, workspace_id=workspace_id, task_set_id=task_set_id)
    if actor_id is not None and db.get(User, actor_id) is None:
        raise NativeLerobotCandidateTokenError("candidate actor is invalid")
    # Lock the scope before its final token/prefix/marker revalidation.  A
    # settings update uses the same row lock, so a scope cannot be revoked or
    # repointed between validation and the batch/catalog write below.
    scope = db.scalar(
        select(ExternalOssImportScope)
        .where(ExternalOssImportScope.id == payload["scope_id"])
        .with_for_update()
    )
    if (
        scope is None
        or not scope.is_enabled
        or scope.workspace_id != workspace_id
        or scope.task_set_id != task_set_id
        or scope.revision != payload["scope_revision"]
        or scope.bucket != payload["bucket"]
        or not _scope_allows_key(scope, payload["marker_key"])
    ):
        raise NativeLerobotCandidateTokenError("candidate scope is unavailable")
    marker = read_native_lerobot_marker(bucket=scope.bucket, marker_key=payload["marker_key"])
    if not _scope_allows_prefix(
        scope,
        marker.marker_key.removesuffix(COMPLETE_MARKER_NAME),
    ):
        raise NativeLerobotCandidateTokenError("candidate scope is unavailable")
    if not hmac.compare_digest(marker.marker_sha256, payload["marker_sha256"]):
        raise NativeLerobotCandidateTokenError("candidate marker has changed")
    existing = db.scalar(
        select(NativeLerobotDataset)
        .where(
            NativeLerobotDataset.workspace_id == workspace_id,
            NativeLerobotDataset.source_oss_uri == marker.oss_uri,
            NativeLerobotDataset.marker_sha256 == marker.marker_sha256,
        )
        .with_for_update()
    )
    if existing is not None:
        raise NativeLerobotDuplicateError(existing.id)
    try:
        with db.begin_nested():
            batch = create_batch(
                db,
                workspace_id=workspace_id,
                task_set_id=task_set_id,
                name=name,
                batch_type="lerobot",
                actor_id=actor_id,
            )
            batch.status = "processing"
            native_dataset = NativeLerobotDataset(
                workspace_id=workspace_id,
                task_set_id=task_set_id,
                batch_id=batch.id,
                name=name.strip(),
                source_oss_uri=marker.oss_uri,
                source_scope_id=scope.id,
                source_scope_revision=scope.revision,
                oss_uri=platform_native_lerobot_uri(
                    workspace_id=workspace_id,
                    task_set_id=task_set_id,
                    batch_id=batch.id,
                    robot_type=marker.robot_type,
                    dataset_id=marker.dataset_id,
                ),
                robot_type=marker.robot_type,
                dataset_id=marker.dataset_id,
                file_count=marker.file_count,
                total_size=marker.total_size,
                completed_at=marker.completed_at.replace(tzinfo=None),
                manifest_sha256=marker.manifest_sha256,
                marker_sha256=marker.marker_sha256,
                objects_json=[
                    {
                        "path": item.path,
                        "size": item.size,
                        "sha256": item.sha256,
                    }
                    for item in marker.objects
                ],
                status="active",
                copy_status="queued",
                created_by_user_id=actor_id,
            )
            db.add(native_dataset)
            db.flush()
            job = create_or_get_job_in_transaction(
                db,
                kind="native_lerobot_copy",
                resource_type="native_lerobot_dataset",
                resource_id=native_dataset.id,
                idempotency_key=f"native-copy:{native_dataset.id}:{marker.marker_sha256}",
                queue="export",
                actor_id=actor_id,
                workspace_id=workspace_id,
                task_set_id=task_set_id,
                detail={
                    "marker_sha256": marker.marker_sha256,
                    "file_count": marker.file_count,
                    "total_size": marker.total_size,
                },
            )
            native_dataset.last_copy_job_id = job.id
            db.flush()
    except IntegrityError as exc:
        db.expire_all()
        existing_id = _existing_native_dataset_id(
            db,
            workspace_id,
            marker.oss_uri,
            marker.marker_sha256,
        )
        raise NativeLerobotDuplicateError(existing_id) from exc
    return batch, native_dataset, job


def platform_native_lerobot_uri(
    *,
    workspace_id: int,
    task_set_id: int,
    batch_id: int,
    native_dataset_id: int | None = None,
    robot_type: str,
    dataset_id: str,
) -> str:
    """Return the only destination prefix allowed for one platform copy."""
    for value, field in (
        (workspace_id, "workspace_id"),
        (task_set_id, "task_set_id"),
        (batch_id, "batch_id"),
    ):
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise NativeLerobotMarkerError(f"{field}_invalid")
    if _ROBOT_TYPE_RE.fullmatch(robot_type) is None or not _safe_dataset_id(dataset_id):
        raise NativeLerobotMarkerError("marker_identity_invalid")
    bucket = oss_client.bucket_name("export")
    _validate_bucket(bucket)
    root = (
        f"oss://{bucket}/exports/v1/native-lerobot/workspaces/{workspace_id}/"
        f"task-sets/{task_set_id}/batches/{batch_id}/"
    )
    if native_dataset_id is not None:
        if (
            not isinstance(native_dataset_id, int)
            or isinstance(native_dataset_id, bool)
            or native_dataset_id <= 0
        ):
            raise NativeLerobotMarkerError("native_dataset_id_invalid")
        root += f"datasets/{native_dataset_id}/"
    return f"{root}{robot_type}/{dataset_id}/"


def native_lerobot_dataset_item(row: NativeLerobotDataset) -> dict[str, object]:
    """Return directory metadata only; URI delivery is a separate audited API."""
    return {
        "id": row.id,
        "type": "native_lerobot",
        "workspace_id": row.workspace_id,
        "task_set_id": row.task_set_id,
        "batch_id": row.batch_id,
        "batch_name": row.batch.name if row.batch is not None else "",
        "source_session": "historical_single_import"
        if row.import_session_id is None
        else "batch_import_session",
        "name": row.name,
        "robot_type": row.robot_type,
        "dataset_id": row.dataset_id,
        "file_count": row.file_count,
        "total_size": row.total_size,
        "completed_at": format_api_datetime(row.completed_at),
        "manifest_sha256": row.manifest_sha256,
        "status": row.status,
        "copy_status": row.copy_status,
        "copy_error_code": row.copy_error_code,
        "copy_error_message": row.copy_error_message,
        "last_copy_job_id": row.last_copy_job_id,
        "oss_uri_available": row.status == "active" and row.copy_status == "succeeded",
        "created_at": format_api_datetime(row.created_at),
        "updated_at": format_api_datetime(row.updated_at),
    }


def _scope_marker_keys(scope: ExternalOssImportScope) -> Iterable[str]:
    for prefix in scope.prefixes_json or []:
        normalized = _strict_prefix(prefix)
        if normalized is None:
            continue
        yield from _marker_keys_for_prefix(scope.bucket, normalized)


def _marker_keys_for_prefix(bucket: str, scope_prefix: str) -> Iterable[str]:
    if LEROBOT_ROOT_PREFIX.startswith(f"{scope_prefix}/") or scope_prefix == LEROBOT_ROOT_PREFIX:
        yield from _marker_keys_from_root(bucket)
        return
    if not scope_prefix.startswith(f"{LEROBOT_ROOT_PREFIX}/"):
        return
    tail = scope_prefix[len(LEROBOT_ROOT_PREFIX) + 1 :].split("/")
    if len(tail) == 1 and _ROBOT_TYPE_RE.fullmatch(tail[0]):
        yield from _marker_keys_for_robot(bucket, tail[0])
    elif len(tail) == 2 and _ROBOT_TYPE_RE.fullmatch(tail[0]) and _safe_dataset_id(tail[1]):
        yield f"{LEROBOT_ROOT_PREFIX}/{tail[0]}/{tail[1]}/{COMPLETE_MARKER_NAME}"


def _marker_keys_from_root(bucket: str) -> Iterable[str]:
    root = f"{LEROBOT_ROOT_PREFIX}/"
    page = oss_client.list_prefix_directory_page(
        bucket,
        root,
        continuation_token=None,
        max_keys=MAX_ROBOT_TYPES,
    )
    if page.next_token is not None:
        raise NativeLerobotMarkerError("candidate_scan_limit")
    for child in page.prefixes:
        robot_type = _direct_child(root, child)
        if robot_type is not None and _ROBOT_TYPE_RE.fullmatch(robot_type):
            yield from _marker_keys_for_robot(bucket, robot_type)


def _marker_keys_for_robot(bucket: str, robot_type: str) -> Iterable[str]:
    root = f"{LEROBOT_ROOT_PREFIX}/{robot_type}/"
    page = oss_client.list_prefix_directory_page(
        bucket,
        root,
        continuation_token=None,
        max_keys=MAX_DATASETS_PER_ROBOT,
    )
    if page.next_token is not None:
        raise NativeLerobotMarkerError("candidate_scan_limit")
    for child in page.prefixes:
        dataset_id = _direct_child(root, child)
        if dataset_id is not None and _safe_dataset_id(dataset_id):
            yield f"{root}{dataset_id}/{COMPLETE_MARKER_NAME}"


def _direct_child(parent: str, value: object) -> str | None:
    if not isinstance(value, str) or not value.startswith(parent) or not value.endswith("/"):
        raise NativeLerobotMarkerError("candidate_listing_invalid")
    child = value[len(parent) : -1]
    if not child or "/" in child or "\\" in child:
        raise NativeLerobotMarkerError("candidate_listing_invalid")
    return child


def _validated_marker(
    *,
    bucket: str,
    marker_key: str,
    root_key: str,
    expected_robot_type: str,
    expected_dataset_id: str,
    payload: object,
    marker_sha256: str,
) -> NativeLerobotMarker:
    if not isinstance(payload, dict) or set(payload) != _REQUIRED_MARKER_KEYS:
        raise NativeLerobotMarkerError("marker_schema_invalid")
    if payload.get("schema_version") != COMPLETE_SCHEMA_VERSION or isinstance(
        payload.get("schema_version"), bool
    ):
        raise NativeLerobotMarkerError("marker_schema_unsupported")
    robot_type = payload.get("robot_type")
    dataset_id = payload.get("dataset_id")
    if robot_type != expected_robot_type or dataset_id != expected_dataset_id:
        raise NativeLerobotMarkerError("marker_identity_invalid")
    if not isinstance(robot_type, str) or not _ROBOT_TYPE_RE.fullmatch(robot_type):
        raise NativeLerobotMarkerError("marker_identity_invalid")
    if not isinstance(dataset_id, str) or not _safe_dataset_id(dataset_id):
        raise NativeLerobotMarkerError("marker_identity_invalid")
    objects = payload.get("objects")
    if not isinstance(objects, list) or len(objects) > 100_000:
        raise NativeLerobotMarkerError("marker_objects_invalid")
    normalized_objects: list[NativeLerobotObject] = []
    previous_path = ""
    total_size = 0
    for item in objects:
        if not isinstance(item, dict) or set(item) != {"path", "size", "sha256"}:
            raise NativeLerobotMarkerError("marker_objects_invalid")
        path = item.get("path")
        size = item.get("size")
        digest = item.get("sha256")
        if (
            not isinstance(path, str)
            or not _safe_object_path(path)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size < 0
            or not isinstance(digest, str)
            or _SHA256_RE.fullmatch(digest) is None
            or (previous_path and path <= previous_path)
        ):
            raise NativeLerobotMarkerError("marker_objects_invalid")
        previous_path = path
        if size > MAX_PERSISTED_TOTAL_SIZE - total_size:
            raise NativeLerobotMarkerError("marker_summary_invalid")
        total_size += size
        normalized_objects.append(NativeLerobotObject(path=path, size=size, sha256=digest))
    file_count = payload.get("file_count")
    declared_total_size = payload.get("total_size")
    if (
        not isinstance(file_count, int)
        or isinstance(file_count, bool)
        or not isinstance(declared_total_size, int)
        or isinstance(declared_total_size, bool)
        or file_count != len(normalized_objects)
        or declared_total_size != total_size
    ):
        raise NativeLerobotMarkerError("marker_summary_invalid")
    manifest_sha256 = payload.get("manifest_sha256")
    expected_manifest_sha256 = hashlib.sha256(
        _canonical_json(
            {
                "robot_type": robot_type,
                "dataset_id": dataset_id,
                "objects": [
                    {"path": item.path, "size": item.size, "sha256": item.sha256}
                    for item in normalized_objects
                ],
            }
        )
    ).hexdigest()
    if not isinstance(manifest_sha256, str) or not hmac.compare_digest(
        manifest_sha256, expected_manifest_sha256
    ):
        raise NativeLerobotMarkerError("marker_manifest_invalid")
    completed_at = _parse_completed_at(payload.get("completed_at"))
    return NativeLerobotMarker(
        bucket=bucket,
        marker_key=marker_key,
        oss_uri=f"oss://{bucket}/{root_key}/",
        robot_type=robot_type,
        dataset_id=dataset_id,
        file_count=file_count,
        total_size=declared_total_size,
        completed_at=completed_at,
        manifest_sha256=manifest_sha256,
        marker_sha256=marker_sha256,
        objects=tuple(normalized_objects),
    )


def _identity_from_marker_key(marker_key: object) -> tuple[str, str, str]:
    if not isinstance(marker_key, str) or marker_key != marker_key.strip() or "\\" in marker_key:
        raise NativeLerobotMarkerError("marker_path_invalid")
    parts = marker_key.split("/")
    if (
        parts[:3] != ["prod", "raw", "robot"]
        or len(parts) != 6
        or parts[-1] != COMPLETE_MARKER_NAME
    ):
        raise NativeLerobotMarkerError("marker_path_invalid")
    robot_type, dataset_id = parts[3], parts[4]
    if _ROBOT_TYPE_RE.fullmatch(robot_type) is None or not _safe_dataset_id(dataset_id):
        raise NativeLerobotMarkerError("marker_path_invalid")
    return robot_type, dataset_id, "/".join(parts[:-1])


def _safe_dataset_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 256
        and value not in {".", ".."}
        and "/" not in value
        and "\\" not in value
        and "\x00" not in value
        and value == value.strip()
    )


def _safe_object_path(value: str) -> bool:
    if not value.startswith(_ALLOWED_OBJECT_ROOTS) or "\\" in value or "\x00" in value:
        return False
    try:
        path = PurePosixPath(value)
    except ValueError:
        return False
    return (
        not path.is_absolute()
        and len(path.parts) >= 2
        and all(part not in {"", ".", ".."} for part in path.parts)
    )


def _parse_completed_at(value: object) -> datetime:
    if not isinstance(value, str) or _COMPLETED_AT_RE.fullmatch(value) is None:
        raise NativeLerobotMarkerError("marker_completed_at_invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError as exc:
        raise NativeLerobotMarkerError("marker_completed_at_invalid") from exc
    return parsed.replace(tzinfo=timezone.utc)


def _metadata_sha256(metadata: object) -> str | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get("x-oss-meta-sha256")
    if not isinstance(value, str) or _SHA256_RE.fullmatch(value) is None:
        return None
    return value


def _strict_prefix(value: object) -> str | None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\\" in value
        or "\x00" in value
    ):
        return None
    normalized = value.strip("/")
    if not normalized or any(part in {"", ".", ".."} for part in normalized.split("/")):
        return None
    return normalized


def _scope_allows_key(scope: ExternalOssImportScope, key: str) -> bool:
    return any(
        key == prefix or key.startswith(f"{prefix}/")
        for raw_prefix in scope.prefixes_json or []
        if (prefix := _strict_prefix(raw_prefix)) is not None
    )


def _scope_allows_prefix(scope: ExternalOssImportScope, prefix: str) -> bool:
    """Require one authorized prefix to cover an entire native dataset tree."""
    normalized = _strict_prefix(prefix.rstrip("/"))
    if normalized is None:
        return False
    return any(
        normalized == allowed or normalized.startswith(f"{allowed}/")
        for raw_prefix in scope.prefixes_json or []
        if (allowed := _strict_prefix(raw_prefix)) is not None
    )


def _validate_bucket(bucket: object) -> None:
    if (
        not isinstance(bucket, str)
        or not bucket
        or len(bucket) > 63
        or "/" in bucket
        or "\\" in bucket
    ):
        raise NativeLerobotMarkerError("marker_path_invalid")


def _require_task_set_scope(db: Session, *, workspace_id: int, task_set_id: int) -> None:
    task_set = db.get(TaskSet, task_set_id)
    if task_set is None or task_set.workspace_id != workspace_id:
        raise NativeLerobotCandidateTokenError("task set scope is invalid")


def _canonical_json(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )


def _encode_candidate_token(
    *,
    workspace_id: int,
    task_set_id: int,
    actor_id: int,
    scope: ExternalOssImportScope,
    marker: NativeLerobotMarker,
) -> str:
    if not isinstance(actor_id, int) or isinstance(actor_id, bool) or actor_id <= 0:
        raise NativeLerobotCandidateTokenError("candidate actor is invalid")
    claims = {
        "v": 2,
        "actor_id": actor_id,
        "workspace_id": workspace_id,
        "task_set_id": task_set_id,
        "scope_id": scope.id,
        "scope_revision": scope.revision,
        "bucket": marker.bucket,
        "marker_key": marker.marker_key,
        "marker_sha256": marker.marker_sha256,
    }
    reference = secrets.token_urlsafe(32)
    key = _candidate_reference_key(reference)
    with redis_service.operation() as client:
        stored = client.set(
            key, _canonical_json(claims).decode("utf-8"), ex=CANDIDATE_TOKEN_TTL_SECONDS, nx=True
        )
    if not stored:
        raise NativeLerobotCandidateTokenError("candidate token service is unavailable")
    return reference


def _decode_candidate_token(value: object, *, actor_id: int | None) -> dict[str, object]:
    if (
        not isinstance(value, str)
        or _CANDIDATE_REFERENCE_RE.fullmatch(value) is None
        or not isinstance(actor_id, int)
        or isinstance(actor_id, bool)
        or actor_id <= 0
    ):
        raise NativeLerobotCandidateTokenError("candidate token is invalid")
    key = _candidate_reference_key(value)
    with redis_service.operation() as client:
        raw = client.get(key)
    if raw is None:
        raise NativeLerobotCandidateTokenError("candidate token has expired")
    try:
        claims = json.loads(raw)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as exc:
        raise NativeLerobotCandidateTokenError("candidate token is invalid") from exc
    required = {
        "v",
        "actor_id",
        "workspace_id",
        "task_set_id",
        "scope_id",
        "scope_revision",
        "bucket",
        "marker_key",
        "marker_sha256",
    }
    if not isinstance(claims, dict) or set(claims) != required or claims.get("v") != 2:
        raise NativeLerobotCandidateTokenError("candidate token is invalid")
    numeric_fields = ("actor_id", "workspace_id", "task_set_id", "scope_id", "scope_revision")
    if any(
        not isinstance(claims.get(field), int) or isinstance(claims.get(field), bool)
        for field in numeric_fields
    ):
        raise NativeLerobotCandidateTokenError("candidate token is invalid")
    if claims["actor_id"] != actor_id:
        raise NativeLerobotCandidateTokenError("candidate token is invalid")
    if not isinstance(claims.get("bucket"), str) or not isinstance(claims.get("marker_key"), str):
        raise NativeLerobotCandidateTokenError("candidate token is invalid")
    if (
        not isinstance(claims.get("marker_sha256"), str)
        or _SHA256_RE.fullmatch(claims["marker_sha256"]) is None
    ):
        raise NativeLerobotCandidateTokenError("candidate token is invalid")
    with redis_service.operation() as client:
        consumed = client.getdel(key)
    if consumed != raw:
        raise NativeLerobotCandidateTokenError("candidate token has expired")
    return claims


def _candidate_reference_key(reference: str) -> str:
    return f"{_CANDIDATE_REFERENCE_PREFIX}{reference}"


def _existing_native_dataset_id(
    db: Session,
    workspace_id: int,
    source_oss_uri: str,
    marker_sha256: str,
) -> int:
    existing = db.scalar(
        select(NativeLerobotDataset.id).where(
            NativeLerobotDataset.workspace_id == workspace_id,
            NativeLerobotDataset.source_oss_uri == source_oss_uri,
            NativeLerobotDataset.marker_sha256 == marker_sha256,
        )
    )
    return int(existing or 0)
