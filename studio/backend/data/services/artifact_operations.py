"""Durable, bounded coordination for database rows and external artifacts."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session

from data.database import ArtifactOperation, EpisodeArtifact, JobRun
from data.services.storage_mode import uses_cloud_uri_authority
from data.utils.storage_paths import is_under_storage_root, storage_root_path
from data.utils.storage_uri import parse_storage_uri

OPERATION_KINDS = frozenset(
    {
        "import_original_publish",
        "raw_source_publish",
        "process_preview_publish",
        "official_publish",
    }
)
OPERATION_STATUSES = frozenset({"pending", "published", "cleanup_pending", "cleaned"})
_ROLE_PREFIXES = {
    "raw": ("raw/v1/", "raw/v2/"),
    "process": ("process/v1/", "process/v2/"),
    "official": ("official/v1/", "official/v2/"),
    "export": ("exports/v1/",),
}


def ensure_artifact_operation(
    db: Session,
    *,
    artifact: EpisodeArtifact,
    job: JobRun | None,
    operation_kind: str,
    source_path: Path | None = None,
    checksum_sha256: str = "",
    size_bytes: int = 0,
    manifest_json: dict[str, object] | None = None,
) -> ArtifactOperation:
    """Persist one exact external-write intent before performing I/O."""
    if operation_kind not in OPERATION_KINDS:
        raise ValueError("unsupported artifact operation kind")
    _validate_target_uri(artifact)
    if source_path is not None:
        source = _operation_source_path(source_path)
        checksum, size, manifest = _path_manifest(source)
    else:
        checksum = str(checksum_sha256 or "").strip().lower()
        size = int(size_bytes)
        manifest = dict(manifest_json or {})
        if len(checksum) != 64 or size <= 0 or not isinstance(manifest, dict):
            raise ValueError("artifact operation source identity is invalid")
    existing = db.scalar(
        select(ArtifactOperation).where(
            ArtifactOperation.artifact_id == artifact.id,
            ArtifactOperation.operation_kind == operation_kind,
        )
    )
    if existing is not None:
        if existing.target_uri != artifact.storage_uri:
            raise ValueError("artifact operation target changed")
        if existing.checksum_sha256 and existing.checksum_sha256 != checksum:
            raise ValueError("artifact operation source changed")
        if existing.status == "cleaned":
            existing.status = "pending"
        if not existing.checksum_sha256:
            existing.checksum_sha256 = checksum
            existing.size_bytes = size
            existing.manifest_json = manifest
        if job is not None and existing.job_id is None:
            existing.job_id = job.id
        db.flush()
        return existing
    operation = ArtifactOperation(
        id=uuid4().hex,
        artifact_id=artifact.id,
        job_id=job.id if job is not None else None,
        operation_kind=operation_kind,
        status="pending",
        target_uri=artifact.storage_uri,
        checksum_sha256=checksum,
        size_bytes=size,
        manifest_json=manifest,
    )
    db.add(operation)
    db.flush()
    return operation


def materialize_artifact_operation(
    db: Session,
    *,
    operation_id: str,
    source_path: Path,
) -> ArtifactOperation:
    """Write one operation target without overwriting an immutable object."""
    operation = db.get(ArtifactOperation, operation_id)
    if operation is None:
        raise ValueError("artifact operation does not exist")
    if operation.status == "published":
        return operation
    if operation.status not in {"pending", "cleanup_pending"}:
        raise ValueError("artifact operation is not writable")
    source = _operation_source_path(source_path)
    checksum, size, manifest = _path_manifest(source)
    if checksum != operation.checksum_sha256 or size != operation.size_bytes:
        raise ValueError("artifact operation source no longer matches its intent")
    bucket, key = _target_bucket_key(operation.target_uri)
    from data.infra import oss_client

    if uses_cloud_uri_authority() and operation.target_uri.startswith("oss://"):
        target_state = _cloud_target_state(bucket, key, manifest)
        if target_state == "partial":
            if not _partial_cloud_target_matches_source(
                operation_id=operation.id,
                source=source,
                bucket=bucket,
                key=key,
                manifest=manifest,
            ):
                raise FileExistsError("artifact operation target already contains different data")
            operation = _clear_verified_partial_cloud_target(db, operation_id=operation.id)
            target_state = _cloud_target_state(bucket, key, manifest)
        if target_state == "absent":
            oss_client.upload_file(source, bucket, key, forbid_overwrite=True)
            if _cloud_target_state(bucket, key, manifest) != "present":
                raise ValueError("external artifact write could not be verified")
        elif target_state == "present":
            if not _cloud_target_matches_source(
                operation_id=operation.id,
                source=source,
                bucket=bucket,
                key=key,
                manifest=manifest,
            ):
                raise FileExistsError("artifact operation target already contains different data")
        else:
            raise FileExistsError("artifact operation target already contains different data")
    else:
        destination = (storage_root_path() / key).resolve()
        if not is_under_storage_root(destination):
            raise ValueError("artifact operation target is outside storage root")
        if destination.exists() or destination.is_symlink():
            if not _local_matches(source, destination):
                raise FileExistsError("artifact operation target already contains different data")
        else:
            destination.parent.mkdir(parents=True, exist_ok=True)
            if source.is_dir():
                shutil.copytree(source, destination)
            else:
                shutil.copy2(source, destination)
    operation.manifest_json = manifest
    operation.error_code = ""
    operation.error_message = ""
    db.flush()
    return operation


def complete_artifact_operation(db: Session, *, operation_id: str) -> ArtifactOperation:
    operation = db.get(ArtifactOperation, operation_id)
    if operation is None:
        raise ValueError("artifact operation does not exist")
    if operation.status == "cleaned":
        raise ValueError("cleaned artifact operation cannot be published")
    operation.status = "published"
    operation.error_code = ""
    operation.error_message = ""
    db.flush()
    return operation


def mark_artifact_cleanup_pending(
    db: Session, *, operation_id: str, error: Exception
) -> ArtifactOperation:
    operation = db.get(ArtifactOperation, operation_id)
    if operation is None:
        raise ValueError("artifact operation does not exist")
    if operation.status != "published":
        operation.status = "cleanup_pending"
    operation.error_code = "artifact_cleanup_pending"
    operation.error_message = str(error)[:1000]
    db.flush()
    return operation


def cleanup_artifact_operation(db: Session, *, operation_id: str) -> ArtifactOperation:
    """Delete only manifest-listed objects for an explicit cleanup request."""
    operation = db.get(ArtifactOperation, operation_id)
    if operation is None:
        raise ValueError("artifact operation does not exist")
    if operation.status == "published":
        return operation
    _delete_operation_target_objects(operation)
    operation.status = "cleaned"
    operation.error_code = ""
    operation.error_message = ""
    db.flush()
    return operation


def _clear_verified_partial_cloud_target(db: Session, *, operation_id: str) -> ArtifactOperation:
    """Remove a verified partial directory before retrying its immutable write.

    This path is only used when every existing file was proven to match the
    durable source manifest.  The commit before deletion makes an interrupted
    cleanup observable and lets the same operation resume without touching a
    Batch or Episode prefix.
    """
    operation = db.get(ArtifactOperation, operation_id)
    if operation is None:
        raise ValueError("artifact operation does not exist")
    if operation.status not in {"pending", "cleanup_pending"}:
        raise ValueError("artifact operation cannot recover a partial target")
    operation.status = "cleanup_pending"
    operation.error_code = "artifact_partial_target"
    operation.error_message = "recovering a verified partial immutable target"
    db.commit()

    operation = db.get(ArtifactOperation, operation_id)
    if operation is None:
        raise ValueError("artifact operation does not exist")
    _delete_operation_target_objects(operation)
    operation.status = "pending"
    operation.error_code = ""
    operation.error_message = ""
    db.commit()
    recovered = db.get(ArtifactOperation, operation_id)
    if recovered is None:
        raise ValueError("artifact operation does not exist")
    return recovered


def _delete_operation_target_objects(operation: ArtifactOperation) -> None:
    """Delete only the exact keys declared by one operation manifest."""
    bucket, key = _target_bucket_key(operation.target_uri)
    from data.infra import oss_client

    manifest = operation.manifest_json if isinstance(operation.manifest_json, dict) else {}
    keys = manifest.get("keys") if isinstance(manifest.get("keys"), list) else []
    if not keys:
        keys = [""]
    for relative in keys:
        if (
            not isinstance(relative, str)
            or relative.startswith("/")
            or ".." in Path(relative).parts
        ):
            raise ValueError("artifact cleanup manifest is invalid")
        target_key = key if not relative else f"{key.rstrip('/')}/{relative.lstrip('/')}"
        if operation.target_uri.startswith("oss://"):
            oss_client.delete_object(bucket, target_key)
        else:
            local_target = (storage_root_path() / target_key).resolve()
            if not is_under_storage_root(local_target):
                raise ValueError("artifact cleanup target is outside storage root")
            if local_target.is_file() or local_target.is_symlink():
                local_target.unlink(missing_ok=True)


def _validate_target_uri(artifact: EpisodeArtifact) -> None:
    uri = str(artifact.storage_uri or "")
    if not uri.startswith(("nas://", "oss://")):
        raise ValueError("artifact target URI is invalid")
    if uri.startswith("nas://"):
        key = uri[len("nas://") :]
    else:
        _bucket, key = parse_storage_uri(uri)
    prefixes = _ROLE_PREFIXES.get(str(artifact.storage_role))
    if not prefixes or not key.startswith(prefixes) or not key:
        raise ValueError("artifact target is outside its storage role")


def _target_bucket_key(uri: str) -> tuple[str, str]:
    if uri.startswith("nas://"):
        key = uri[len("nas://") :]
        if not key:
            raise ValueError("artifact target URI is invalid")
        return "", key
    bucket, key = parse_storage_uri(uri)
    if not bucket or not key:
        raise ValueError("artifact target URI is invalid")
    return bucket, key


def _operation_source_path(value: Path | str) -> Path:
    """Resolve a server-owned source without allowing a symlinked root."""
    candidate = Path(value)
    if candidate.is_symlink():
        raise ValueError("artifact operation source is a symlink")
    source = candidate.resolve()
    if not source.exists() or not is_under_storage_root(source):
        raise ValueError("artifact operation source is outside storage root")
    return source


def _path_manifest(path: Path) -> tuple[str, int, dict[str, object]]:
    candidate = Path(path)
    if candidate.is_symlink():
        raise ValueError("artifact source is a symlink")
    path = candidate.resolve()
    if not path.exists() or not is_under_storage_root(path):
        raise ValueError("artifact source is unavailable")
    entries: list[dict[str, object]] = []
    is_file = path.is_file()
    if is_file:
        entries.append({"path": path.name, "sha256": _sha256(path), "size": path.stat().st_size})
    else:
        if not path.is_dir():
            raise ValueError("artifact source is not a regular file or directory")
        for item in sorted(path.rglob("*")):
            if item.is_symlink():
                raise ValueError("artifact source contains a symlink")
            if item.is_dir():
                continue
            if not item.is_file():
                raise ValueError("artifact source contains a non-regular entry")
            relative = item.relative_to(path).as_posix()
            entries.append({"path": relative, "sha256": _sha256(item), "size": item.stat().st_size})
    digest = hashlib.sha256(
        json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    size = sum(int(item["size"]) for item in entries)
    return (
        digest,
        size,
        {
            "kind": "file" if is_file else "directory",
            "entries": entries,
            "keys": [] if is_file else [str(item["path"]) for item in entries],
        },
    )


def _cloud_target_state(bucket: str, key: str, manifest: dict[str, object]) -> str:
    """Return absent, partial, present, or conflict for one immutable target."""
    from data.infra import oss_client

    entries = manifest.get("entries") if isinstance(manifest, dict) else None
    if not isinstance(entries, list) or not entries:
        return "conflict"
    kind = manifest.get("kind") if isinstance(manifest, dict) else ""
    present_count = 0
    missing_count = 0
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            return "conflict"
        target_key = key if kind == "file" else f"{key.rstrip('/')}/{entry['path']}"
        info = oss_client.object_info(bucket, target_key)
        if info is None:
            # A provider test double may expose existence without a bounded
            # HEAD identity. The real client returns OSSObjectInfo here.
            if oss_client.object_exists(bucket, target_key):
                present_count += 1
            else:
                missing_count += 1
        elif int(info.size) != int(entry.get("size", -1)):
            return "conflict"
        else:
            present_count += 1
    if present_count == 0:
        return "absent"
    return "present" if missing_count == 0 else "partial"


def _cloud_target_matches_source(
    *,
    operation_id: str,
    source: Path,
    bucket: str,
    key: str,
    manifest: dict[str, object],
) -> bool:
    """Verify a recovered cloud target byte-for-byte before reusing it."""
    from data.infra import oss_client

    if not operation_id.isalnum() or len(operation_id) > 64:
        raise ValueError("artifact operation id is invalid")
    verification = storage_root_path() / "process" / "artifact-verification" / operation_id
    verification = verification.resolve()
    if (
        not is_under_storage_root(verification)
        or verification.parent.name != "artifact-verification"
    ):
        raise ValueError("artifact verification path is outside storage root")
    if verification.exists() or verification.is_symlink():
        if verification.is_symlink():
            raise ValueError("artifact verification path is a symlink")
        shutil.rmtree(verification)
    verification.mkdir(parents=True, exist_ok=False)
    try:
        oss_client.download_to(verification, bucket, key)
        target = verification / Path(key).name if manifest.get("kind") == "file" else verification
        return _local_matches(source, target)
    finally:
        if verification.exists() or verification.is_symlink():
            if verification.is_symlink() or verification.parent.name != "artifact-verification":
                raise ValueError("artifact verification cleanup target is invalid")
            shutil.rmtree(verification)


def _partial_cloud_target_matches_source(
    *,
    operation_id: str,
    source: Path,
    bucket: str,
    key: str,
    manifest: dict[str, object],
) -> bool:
    """Verify every existing object in a partial directory before deletion."""
    if manifest.get("kind") != "directory" or source.is_file():
        return False
    verification = _verification_dir(operation_id)
    verification.mkdir(parents=True, exist_ok=False)
    try:
        from data.infra import oss_client

        oss_client.download_to(verification, bucket, key)
        _target_checksum, _target_size, target_manifest = _path_manifest(verification)
        expected_entries = {
            str(entry["path"]): (str(entry["sha256"]), int(entry["size"]))
            for entry in manifest.get("entries", [])
            if isinstance(entry, dict)
            and isinstance(entry.get("path"), str)
            and isinstance(entry.get("sha256"), str)
            and isinstance(entry.get("size"), int)
        }
        if not expected_entries:
            return False
        for entry in target_manifest.get("entries", []):
            if not isinstance(entry, dict):
                return False
            path = entry.get("path")
            expected = expected_entries.get(path) if isinstance(path, str) else None
            if expected is None or expected != (entry.get("sha256"), entry.get("size")):
                return False
        return True
    finally:
        _remove_verification_dir(verification)


def _verification_dir(operation_id: str) -> Path:
    if not operation_id.isalnum() or len(operation_id) > 64:
        raise ValueError("artifact operation id is invalid")
    verification = storage_root_path() / "process" / "artifact-verification" / operation_id
    verification = verification.resolve()
    if (
        not is_under_storage_root(verification)
        or verification.parent.name != "artifact-verification"
    ):
        raise ValueError("artifact verification path is outside storage root")
    if verification.exists() or verification.is_symlink():
        if verification.is_symlink():
            raise ValueError("artifact verification path is a symlink")
        shutil.rmtree(verification)
    return verification


def _remove_verification_dir(verification: Path) -> None:
    if verification.exists() or verification.is_symlink():
        if verification.is_symlink() or verification.parent.name != "artifact-verification":
            raise ValueError("artifact verification cleanup target is invalid")
        shutil.rmtree(verification)


def _local_matches(source: Path, target: Path) -> bool:
    if source.is_file() != target.is_file():
        return False
    if source.is_file():
        return source.stat().st_size == target.stat().st_size and _sha256(source) == _sha256(target)
    source_checksum, source_size, _manifest = _path_manifest(source)
    target_checksum, target_size, _target_manifest = _path_manifest(target)
    return source_checksum == target_checksum and source_size == target_size


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()
